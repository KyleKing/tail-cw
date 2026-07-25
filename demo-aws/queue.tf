resource "aws_sqs_queue" "dlq" {
  name                      = "${local.prefix}-work-dlq"
  message_retention_seconds = 3600
}

# visibility_timeout_seconds must exceed the worker's 25s timeout, and three receives at this
# timeout put a poison message in the DLQ inside about a minute, which fits a 15-minute demo.
resource "aws_sqs_queue" "work" {
  name                       = "${local.prefix}-work"
  visibility_timeout_seconds = 30
  message_retention_seconds  = 3600

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq.arn
    maxReceiveCount     = 3
  })
}

resource "aws_sqs_queue_redrive_allow_policy" "dlq" {
  queue_url = aws_sqs_queue.dlq.id

  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.work.arn]
  })
}

# batch_size 1 so one poison message fails exactly one invocation. Larger batches would drag
# healthy messages through the same retries and muddy the DLQ story.
resource "aws_lambda_event_source_mapping" "worker" {
  event_source_arn = aws_sqs_queue.work.arn
  function_name    = aws_lambda_function.service["worker"].arn
  batch_size       = 1
  enabled          = var.schedule_enabled
}
