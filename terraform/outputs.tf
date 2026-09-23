output "dashboard_url" {
  value = "https://app.${var.datadog_site}${datadog_dashboard_json.agentic.url}"
}

output "monitor_ids" {
  value = {
    silent_failures = datadog_monitor.silent_failures.id
    firewall_blocks = datadog_monitor.firewall_blocks.id
    budget          = datadog_monitor.budget.id
    tool_loops      = datadog_monitor.tool_loops.id
  }
}
