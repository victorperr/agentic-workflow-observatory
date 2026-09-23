resource "datadog_dashboard_json" "agentic" {
  dashboard = templatefile("${path.module}/dashboard.json.tftpl", {
    ml_app = var.ml_app
    site   = var.datadog_site
  })
}
