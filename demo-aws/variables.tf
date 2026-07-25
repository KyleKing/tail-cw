variable "region" {
  description = "AWS region to deploy into. Every resource is regional, so this also decides where tail-cw must point."
  type        = string
  default     = "us-east-1"
}

variable "name_prefix" {
  description = "Prefix on every resource name. Log groups become /aws/lambda/<prefix>-<service>."
  type        = string
  default     = "tail-cw-demo"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,30}$", var.name_prefix))
    error_message = "name_prefix must be lowercase letters, digits, and hyphens, starting with a letter."
  }
}

variable "run_minutes" {
  description = "How long the schedule keeps generating traffic. It stops on its own after this, whether or not you remember to destroy."
  type        = number
  default     = 15

  validation {
    condition     = var.run_minutes >= 1 && var.run_minutes <= 120
    error_message = "run_minutes must be between 1 and 120; this stack is meant to be short-lived."
  }
}

variable "requests_per_minute" {
  description = "Simulated API requests the gateway issues per minute. Each one fans out to up to three downstream services."
  type        = number
  default     = 90

  validation {
    condition     = var.requests_per_minute >= 10 && var.requests_per_minute <= 240
    error_message = "requests_per_minute must be between 10 and 240 to stay inside the free tier and inside a 60s invocation."
  }
}

variable "log_retention_days" {
  description = "Retention on every demo log group. One day keeps a forgotten teardown from accruing storage."
  type        = number
  default     = 1
}

variable "schedule_enabled" {
  description = "Set false to keep the infrastructure but stop generating traffic."
  type        = bool
  default     = true
}
