data "aws_iam_policy_document" "assume_scheduler" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

data "aws_iam_policy_document" "scheduler" {
  statement {
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.service["gateway"].arn]
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "${local.prefix}-scheduler"
  assume_role_policy = data.aws_iam_policy_document.assume_scheduler.json
}

resource "aws_iam_role_policy" "scheduler" {
  name   = "${local.prefix}-scheduler"
  role   = aws_iam_role.scheduler.id
  policy = data.aws_iam_policy_document.scheduler.json
}

# end_date is the safety net: traffic stops on its own after run_minutes even if the stack is
# left standing. Re-running a finished demo needs a fresh timestamp:
#   tofu apply -replace=time_static.run
resource "aws_scheduler_schedule" "gateway" {
  name       = "${local.prefix}-gateway"
  state      = var.schedule_enabled ? "ENABLED" : "DISABLED"
  group_name = "default"

  schedule_expression          = "rate(1 minute)"
  schedule_expression_timezone = "UTC"
  start_date                   = time_static.run.rfc3339
  end_date                     = timeadd(time_static.run.rfc3339, "${var.run_minutes}m")

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.service["gateway"].arn
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({ source = "schedule" })

    retry_policy {
      maximum_retry_attempts = 0
    }
  }
}
