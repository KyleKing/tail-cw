"""AWS CloudWatch Logs integration.

This module provides async functions for downloading logs from AWS CloudWatch
Logs using aiobotocore. It handles pagination, time range filtering, CloudWatch
filter patterns, and AWS credential chain resolution (profiles, environment
variables, SSO, IAM roles). Clients come from a :class:`ClientPool` held open by
the caller rather than built per call.
"""

from tail_cw.aws.client import ClientPool, ClientProvider, LogEvent, client_pool, fetch_log_events
from tail_cw.aws.live_tail import LiveTailSessionError, stream_live_tail

__all__ = [
    'ClientPool',
    'ClientProvider',
    'LiveTailSessionError',
    'LogEvent',
    'client_pool',
    'fetch_log_events',
    'stream_live_tail',
]
