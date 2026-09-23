variable "datadog_api_key" {
  type      = string
  sensitive = true
}

variable "datadog_app_key" {
  type      = string
  sensitive = true
}

variable "datadog_site" {
  type    = string
  default = "datadoghq.com"
}

variable "ml_app" {
  description = "Must match the action's ml-app input."
  type        = string
  default     = "github-agentic-workflows"
}

variable "notify" {
  description = "Monitor notification handles, e.g. [\"@slack-agentic-ops\", \"@team@example.com\"]."
  type        = list(string)
  default     = []
}

variable "budget_warning_ratio" {
  description = "Alert when a workflow's average budget utilization over 1h exceeds this ratio."
  type        = number
  default     = 0.8
}
