data "aws_caller_identity" "current" {}

# Stamped once at apply. Everything time-dependent hangs off it: the schedule's end date and
# the RUN_EPOCH the handler uses to decide which phase of the incident it is in. To start a
# fresh run, replace it: tofu apply -replace=time_static.run
resource "time_static" "run" {}

locals {
  prefix          = var.name_prefix
  function_prefix = "${local.prefix}-"
  account_id      = data.aws_caller_identity.current.account_id
  emf_namespace   = "TailCwDemo"

  # can_invoke / can_send / consumes drive both the IAM policy and the wiring, so a service
  # never holds a permission its role in the demo does not need.
  services = {
    gateway = {
      timeout_seconds = 55
      memory_mb       = 256
      can_invoke      = true
      can_send        = true
      consumes        = false
    }
    orders = {
      timeout_seconds = 15
      memory_mb       = 128
      can_invoke      = false
      can_send        = false
      consumes        = false
    }
    payments = {
      timeout_seconds = 15
      memory_mb       = 128
      can_invoke      = false
      can_send        = false
      consumes        = false
    }
    inventory = {
      timeout_seconds = 15
      memory_mb       = 128
      can_invoke      = false
      can_send        = false
      consumes        = false
    }
    worker = {
      timeout_seconds = 25
      memory_mb       = 128
      can_invoke      = false
      can_send        = false
      consumes        = true
    }
  }

  log_group_names = { for name, _ in local.services : name => "/aws/lambda/${local.function_prefix}${name}" }

  # One threshold per concern, shared between the alarm and the dashboard annotation so the
  # line drawn on the chart is always the line that fires.
  thresholds = {
    payment_errors_per_minute = 3
    request_latency_p99_ms    = 1500
    dlq_messages              = 0
  }
}

data "archive_file" "lambda" {
  type        = "zip"
  source_dir  = "${path.module}/src"
  output_path = "${path.module}/.build/lambda.zip"
  excludes    = ["__pycache__"]
}
