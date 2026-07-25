resource "aws_cloudwatch_log_group" "service" {
  for_each = local.services

  name              = local.log_group_names[each.key]
  retention_in_days = var.log_retention_days
}

data "aws_iam_policy_document" "assume_lambda" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "service" {
  for_each = local.services

  statement {
    sid       = "WriteOwnLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.service[each.key].arn}:*"]
  }

  # X-Ray's write APIs do not support resource-level permissions, so "*" is the only valid form.
  statement {
    sid       = "WriteTraceSegments"
    actions   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords"]
    resources = ["*"]
  }

  # Scoped by name prefix rather than by function ARN. Referencing the ARNs directly would make
  # the role depend on the functions that depend on the role.
  dynamic "statement" {
    for_each = each.value.can_invoke ? [1] : []

    content {
      sid       = "InvokeDemoServices"
      actions   = ["lambda:InvokeFunction"]
      resources = ["arn:aws:lambda:${var.region}:${local.account_id}:function:${local.function_prefix}*"]
    }
  }

  dynamic "statement" {
    for_each = each.value.can_send ? [1] : []

    content {
      sid       = "EnqueueWork"
      actions   = ["sqs:SendMessage"]
      resources = [aws_sqs_queue.work.arn]
    }
  }

  dynamic "statement" {
    for_each = each.value.consumes ? [1] : []

    content {
      sid       = "ConsumeWork"
      actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
      resources = [aws_sqs_queue.work.arn]
    }
  }
}

resource "aws_iam_role" "service" {
  for_each = local.services

  name               = "${local.prefix}-${each.key}"
  assume_role_policy = data.aws_iam_policy_document.assume_lambda.json
}

resource "aws_iam_role_policy" "service" {
  for_each = local.services

  name   = "${local.prefix}-${each.key}"
  role   = aws_iam_role.service[each.key].id
  policy = data.aws_iam_policy_document.service[each.key].json
}

resource "aws_lambda_function" "service" {
  for_each = local.services

  function_name    = "${local.function_prefix}${each.key}"
  role             = aws_iam_role.service[each.key].arn
  handler          = "handler.lambda_handler"
  runtime          = "python3.13"
  architectures    = ["arm64"]
  timeout          = each.value.timeout_seconds
  memory_size      = each.value.memory_mb
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256

  tracing_config {
    mode = "Active"
  }

  environment {
    variables = {
      SERVICE_NAME        = each.key
      RUN_EPOCH           = tostring(time_static.run.unix)
      FUNCTION_PREFIX     = local.function_prefix
      QUEUE_URL           = aws_sqs_queue.work.url
      EMF_NAMESPACE       = local.emf_namespace
      REQUESTS_PER_MINUTE = tostring(var.requests_per_minute)
    }
  }

  depends_on = [
    aws_cloudwatch_log_group.service,
    aws_iam_role_policy.service,
  ]
}
