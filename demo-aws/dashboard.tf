locals {
  service_names  = keys(local.services)
  function_names = { for name, _ in local.services : name => "${local.function_prefix}${name}" }

  console_logs_url = "https://${var.region}.console.aws.amazon.com/cloudwatch/home?region=${var.region}#logsV2:log-groups"

  # Availability is derived rather than measured: every invocation and error series is pulled in
  # invisibly and folded into one success-rate expression.
  availability_metrics = concat(
    [for idx, name in local.service_names : [
      "AWS/Lambda", "Invocations", "FunctionName", local.function_names[name],
      { id = "i${idx}", stat = "Sum", visible = false }
    ]],
    [for idx, name in local.service_names : [
      "AWS/Lambda", "Errors", "FunctionName", local.function_names[name],
      { id = "e${idx}", stat = "Sum", visible = false }
    ]],
    [[{
      id         = "availability"
      label      = "Success rate %"
      expression = "100*(1-((${join("+", [for idx, _ in local.service_names : "e${idx}"])})/(${join("+", [for idx, _ in local.service_names : "i${idx}"])})))"
    }]],
  )

  dashboard_widgets = [
    {
      type   = "text"
      x      = 0
      y      = 0
      width  = 24
      height = 3
      properties = {
        markdown = join("\n", [
          "# ${local.prefix}: synthetic service under load",
          "",
          "A gateway fans out to `orders`, `inventory`, and `payments`, and enqueues async work for `worker`.",
          "`payments` degrades from minute 6 to 10, recovers by minute 12. Poison messages land in the DLQ.",
          "",
          "Explore it from the terminal: `tail-cw dash ${local.prefix}` · `tail-cw tail @demo` · [Log groups](${local.console_logs_url})",
        ])
      }
    },
    {
      type   = "metric"
      x      = 0
      y      = 3
      width  = 8
      height = 6
      properties = {
        title   = "Traffic: invocations per minute"
        view    = "timeSeries"
        stacked = true
        region  = var.region
        period  = 60
        stat    = "Sum"
        metrics = [
          for name in local.service_names :
          ["AWS/Lambda", "Invocations", "FunctionName", local.function_names[name], { label = name }]
        ]
      }
    },
    {
      type   = "metric"
      x      = 8
      y      = 3
      width  = 8
      height = 6
      properties = {
        title  = "Errors: failed invocations"
        view   = "timeSeries"
        region = var.region
        period = 60
        stat   = "Sum"
        metrics = [
          for name in local.service_names :
          ["AWS/Lambda", "Errors", "FunctionName", local.function_names[name], { label = name }]
        ]
        annotations = {
          horizontal = [{
            label = "payments alarm"
            value = local.thresholds.payment_errors_per_minute
          }]
        }
      }
    },
    {
      type   = "metric"
      x      = 16
      y      = 3
      width  = 8
      height = 6
      properties = {
        title  = "Latency: end-to-end p50 / p90 / p99"
        view   = "timeSeries"
        region = var.region
        period = 60
        metrics = [
          for percentile in ["p50", "p90", "p99"] :
          [local.emf_namespace, "RequestLatencyMs", "service_name", "gateway", { stat = percentile, label = percentile }]
        ]
        annotations = {
          horizontal = [{
            label = "p99 alarm"
            value = local.thresholds.request_latency_p99_ms
          }]
        }
      }
    },
    {
      type   = "metric"
      x      = 0
      y      = 9
      width  = 8
      height = 6
      properties = {
        title   = "Availability: success rate %"
        view    = "timeSeries"
        region  = var.region
        period  = 60
        metrics = local.availability_metrics
        yAxis   = { left = { min = 90, max = 100 } }
      }
    },
    {
      type   = "metric"
      x      = 8
      y      = 9
      width  = 8
      height = 6
      properties = {
        title   = "Saturation: concurrent executions (account)"
        view    = "timeSeries"
        region  = var.region
        period  = 60
        stat    = "Maximum"
        metrics = [["AWS/Lambda", "ConcurrentExecutions", { label = "concurrent" }]]
      }
    },
    {
      type   = "metric"
      x      = 16
      y      = 9
      width  = 8
      height = 6
      properties = {
        title  = "Saturation: queue backlog and dead letters"
        view   = "timeSeries"
        region = var.region
        period = 60
        stat   = "Maximum"
        metrics = [
          ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", aws_sqs_queue.work.name, { label = "work backlog" }],
          ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", aws_sqs_queue.dlq.name, { label = "dead letters" }],
          ["AWS/SQS", "ApproximateAgeOfOldestMessage", "QueueName", aws_sqs_queue.work.name, { label = "oldest message (s)", yAxis = "right" }],
        ]
      }
    },
    {
      type   = "metric"
      x      = 0
      y      = 15
      width  = 12
      height = 6
      properties = {
        title  = "Business: orders placed and payment declines"
        view   = "timeSeries"
        region = var.region
        period = 60
        stat   = "Sum"
        metrics = [
          [local.emf_namespace, "OrdersPlaced", "service_name", "gateway", { label = "orders placed" }],
          [local.emf_namespace, "PaymentDeclines", "service_name", "gateway", { label = "payment declines" }],
          [local.emf_namespace, "OrderValueUsd", "service_name", "gateway", { label = "order value (USD)", yAxis = "right" }],
        ]
      }
    },
    {
      type   = "log"
      x      = 12
      y      = 15
      width  = 12
      height = 6
      properties = {
        title  = "Errors: recent failed requests"
        view   = "table"
        region = var.region
        query = join(" | ", [
          "SOURCE '${local.log_group_names["gateway"]}'",
          "filter level = 'ERROR'",
          "fields @timestamp, route, status_code, duration_ms, trace_id, message",
          "sort @timestamp desc",
          "limit 50",
        ])
      }
    },
    {
      type   = "alarm"
      x      = 0
      y      = 21
      width  = 24
      height = 4
      properties = {
        title = "Alarms"
        alarms = [
          aws_cloudwatch_metric_alarm.payment_errors.arn,
          aws_cloudwatch_metric_alarm.request_latency.arn,
          aws_cloudwatch_metric_alarm.dead_letters.arn,
        ]
      }
    },
  ]
}

resource "aws_cloudwatch_dashboard" "demo" {
  dashboard_name = local.prefix
  dashboard_body = jsonencode({ widgets = local.dashboard_widgets })
}
