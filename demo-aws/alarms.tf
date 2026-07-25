# No SNS topic and no alarm actions on purpose: a topic subscription needs an emailed
# confirmation click, which is exactly the manual first-day step this stack avoids. The alarms
# still change state, which is all the dashboard's alarm widget and tail-cw need.

resource "aws_cloudwatch_metric_alarm" "payment_errors" {
  alarm_name          = "${local.prefix}-payments-errors"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = local.function_names["payments"] }
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  threshold           = local.thresholds.payment_errors_per_minute
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  alarm_description = join("\n", [
    "Detected: the payments function raised more than ${local.thresholds.payment_errors_per_minute} unhandled errors per minute for two minutes.",
    "Meaning: checkout is failing for a real share of requests; the gateway is returning 502 on POST /v1/orders.",
    "Action: open the payments log group and filter on error_type to see which failure mode dominates.",
  ])
}

resource "aws_cloudwatch_metric_alarm" "request_latency" {
  alarm_name          = "${local.prefix}-request-latency-p99"
  namespace           = local.emf_namespace
  metric_name         = "RequestLatencyMs"
  dimensions          = { service_name = "gateway" }
  extended_statistic  = "p99"
  period              = 60
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  threshold           = local.thresholds.request_latency_p99_ms
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  alarm_description = join("\n", [
    "Detected: end-to-end p99 request latency stayed above ${local.thresholds.request_latency_p99_ms}ms for two minutes.",
    "Meaning: the slowest 1% of requests are far outside budget, usually because one downstream service is degraded.",
    "Action: compare per-service AWS/Lambda Duration to find which downstream call is holding the request open.",
  ])
}

resource "aws_cloudwatch_metric_alarm" "dead_letters" {
  alarm_name          = "${local.prefix}-dead-letters"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.dlq.name }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 1
  datapoints_to_alarm = 1
  threshold           = local.thresholds.dlq_messages
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  alarm_description = join("\n", [
    "Detected: at least one message exhausted its three delivery attempts and landed in the dead-letter queue.",
    "Meaning: the worker could not process a task even after retries, so that unit of work is lost until someone redrives it.",
    "Action: read the worker log group for task_failed events and match order_id against the DLQ message body.",
  ])
}
