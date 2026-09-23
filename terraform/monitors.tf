locals {
  notify = join(" ", var.notify)
  tags   = ["managed-by:terraform", "product:agentic-workflow-observatory"]

  # {{workflow.name}} etc. are Datadog template variables, escaped for Terraform with $${}.
  run_link = "LLM traces: https://app.${var.datadog_site}/llm/traces?query=%40ml_app%3A${var.ml_app}%20gh.workflow%3A{{workflow.name}}"
}

resource "datadog_monitor" "silent_failures" {
  name    = "[Agentic] {{workflow.name}} is green in CI but failing silently"
  type    = "query alert"
  query   = "sum(last_1h):sum:agentic.run.silent_failure{*} by {repo,workflow}.as_count() > 0"
  message = <<-EOT
    {{workflow.name}} in {{repo.name}} reported success in GitHub Actions, but the observatory
    found agent-level failures (tool loops, firewall blocks, exhausted budget, or empty or
    invalid output).
    ${local.run_link}
    ${local.notify}
  EOT

  monitor_thresholds {
    critical = 0
  }
  notify_no_data = false
  tags           = local.tags
}

resource "datadog_monitor" "firewall_blocks" {
  name    = "[Agentic] {{workflow.name}} egress blocked by the AWF firewall"
  type    = "query alert"
  query   = "sum(last_1h):sum:agentic.run.firewall.blocked_requests{*} by {repo,workflow}.as_count() > 0"
  message = <<-EOT
    The agent in {{workflow.name}} tried to reach domains outside its `network.allowed` list.
    Either add the domain to the workflow's frontmatter and recompile, or fix the prompt so
    the agent stops trying.
    ${local.run_link}
    ${local.notify}
  EOT

  monitor_thresholds {
    critical = 0
  }
  tags = local.tags
}

resource "datadog_monitor" "budget" {
  name    = "[Agentic] {{workflow.name}} is close to its AI Credits budget"
  type    = "query alert"
  query   = "avg(last_1h):avg:agentic.run.budget_utilization{*} by {repo,workflow} > ${var.budget_warning_ratio}"
  message = <<-EOT
    Runs of {{workflow.name}} use {{value}} of their `max-ai-credits` budget on average.
    Runs that hit the cap stop mid-reasoning and may still exit green.
    ${local.run_link}
    ${local.notify}
  EOT

  monitor_thresholds {
    critical = var.budget_warning_ratio
    warning  = var.budget_warning_ratio * 0.85
  }
  tags = local.tags
}

resource "datadog_monitor" "tool_loops" {
  name    = "[Agentic] {{workflow.name}} agent is looping on tool calls"
  type    = "query alert"
  query   = "sum(last_4h):sum:agentic.run.finding{finding:tool_loop} by {repo,workflow}.as_count() >= 2"
  message = <<-EOT
    Several runs of {{workflow.name}} repeated the same tool call with identical arguments.
    Typical causes: a tool result too large to read, a missing toolset, or an ambiguous prompt.
    ${local.run_link}
    ${local.notify}
  EOT

  monitor_thresholds {
    critical = 2
  }
  tags = local.tags
}
