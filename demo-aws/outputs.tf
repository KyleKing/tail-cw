output "region" {
  description = "Region the demo runs in. Pass it to tail-cw with --region."
  value       = var.region
}

output "dashboard_name" {
  description = "Open it with: tail-cw dash <name> --region <region>"
  value       = aws_cloudwatch_dashboard.demo.dashboard_name
}

output "log_groups" {
  description = "Every demo log group. All five fit inside the ten-group Live Tail limit."
  value       = sort(values(local.log_group_names))
}

output "traffic_stops_at" {
  description = "The schedule stops firing at this time on its own. Use tofu apply -replace=time_static.run to start a new run."
  value       = timeadd(time_static.run.rfc3339, "${var.run_minutes}m")
}

output "tail_cw_config" {
  description = "Drop this into ~/.config/tail-cw/config.toml to get the @demo preset."
  value       = <<-EOT
    [presets]
    demo = ${jsonencode(sort(values(local.log_group_names)))}
  EOT
}
