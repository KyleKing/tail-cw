"""AWS CloudWatch Logs integration.

This module provides functions for downloading logs from AWS CloudWatch Logs
using boto3. It handles pagination, time range filtering, CloudWatch filter
patterns, and AWS credential chain resolution (profiles, environment variables,
IAM roles).
"""

from tail_cw.aws.client import LogEvent, fetch_log_events
from tail_cw.aws.live_tail import LiveTailSessionError, stream_live_tail

__all__ = ['LiveTailSessionError', 'LogEvent', 'fetch_log_events', 'stream_live_tail']
