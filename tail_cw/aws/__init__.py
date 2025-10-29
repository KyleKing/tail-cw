"""AWS CloudWatch Logs integration.

This module provides functions for downloading logs from AWS CloudWatch Logs
using boto3. It handles pagination, time range filtering, CloudWatch filter
patterns, and AWS credential chain resolution (profiles, environment variables,
IAM roles).
"""

from tail_cw.aws.client import LogEvent, fetch_log_events

__all__ = ['LogEvent', 'fetch_log_events']
