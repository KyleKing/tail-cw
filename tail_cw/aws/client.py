"""AWS CloudWatch Logs client module.

Provides streaming access to CloudWatch Logs via boto3 with efficient pagination.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import boto3  # type: ignore[import-untyped]
from botocore.config import Config  # type: ignore[import-untyped]

ProgressCallback = Callable[[int, str], None]


@dataclass(frozen=True)
class LogEvent:
    """Represents a single CloudWatch Logs event.

    Attributes:
        log_group: The CloudWatch log group name.
        log_stream: The CloudWatch log stream name.
        timestamp: Event timestamp as timezone-aware datetime (UTC). Converted from
            epoch milliseconds returned by CloudWatch API.
        message: The log message content.
        event_id: Unique event identifier for deduplication.
        ingestion_time: When CloudWatch ingested the event. May be None if not
            provided in the API response.
    """

    log_group: str
    log_stream: str
    timestamp: datetime
    message: str
    event_id: str
    ingestion_time: datetime | None


def _epoch_ms_to_datetime(epoch_ms: int) -> datetime:
    """Convert epoch milliseconds to timezone-aware datetime in UTC.

    Args:
        epoch_ms: Timestamp in milliseconds since Unix epoch.

    Returns:
        Timezone-aware datetime object in UTC.
    """
    return datetime.fromtimestamp(epoch_ms / 1000.0, tz=UTC)


def _create_logs_client(region_name: str | None = None, profile_name: str | None = None) -> Any:
    """Create a boto3 CloudWatch Logs client with retry configuration.

    Args:
        region_name: Optional AWS region name. If None, uses boto3's default
            region resolution (environment variables, config files, etc.).
        profile_name: Optional AWS profile name. If provided, credentials are
            resolved through a boto3 Session for that profile.

    Returns:
        Configured boto3 CloudWatch Logs client.
    """
    config = Config(
        retries={
            'max_attempts': 10,
            'mode': 'standard',
        },
    )
    if profile_name is not None:
        session = boto3.Session(profile_name=profile_name, region_name=region_name)
        return session.client('logs', config=config)
    return boto3.client('logs', config=config, region_name=region_name)


def fetch_log_events(
    log_group_name: str,
    start_time: datetime,
    end_time: datetime,
    *,
    filter_pattern: str | None = None,
    log_stream_names: list[str] | None = None,
    interleaved: bool = True,
    profile_name: str | None = None,
    region_name: str | None = None,
    progress_callback: ProgressCallback | None = None,
) -> Iterator[LogEvent]:
    """Fetch CloudWatch Logs events using efficient pagination.

    Streams log events from CloudWatch Logs without loading all events into memory.
    Uses boto3's paginator to handle pagination automatically with retry logic for
    throttling errors.

    Args:
        log_group_name: CloudWatch log group name to query.
        start_time: Start of time range (inclusive). Must be timezone-aware.
        end_time: End of time range (inclusive). Must be timezone-aware.
        filter_pattern: Optional CloudWatch Logs filter pattern for server-side
            filtering. See AWS documentation for filter syntax.
        log_stream_names: Optional list of log stream names to filter. If None,
            searches all streams in the log group.
        interleaved: Whether to interleave events from multiple streams
            chronologically. Default is True.
        profile_name: Optional AWS profile name for credential resolution.
        region_name: Optional AWS region override. If None, uses boto3's default
            region resolution.
        progress_callback: Optional callable invoked every 100 events fetched.

    Yields:
        LogEvent instances for each log event in the time range.

    Example:
        >>> from datetime import datetime, timezone, timedelta
        >>> start = datetime.now(tz=timezone.utc) - timedelta(hours=1)
        >>> end = datetime.now(tz=timezone.utc)
        >>> for event in fetch_log_events('/aws/lambda/my-function', start, end):
        ...     print(f"{event.timestamp}: {event.message}")

    Note:
        AWS credentials are resolved using boto3's default credential chain:
        environment variables, AWS profiles, IAM roles, etc. Errors from boto3
        (e.g., access denied, invalid log group) will bubble up to the caller.
        Throttling errors are handled automatically via retry configuration.
    """
    # Convert datetime to epoch milliseconds
    start_time_ms = int(start_time.timestamp() * 1000)
    end_time_ms = int(end_time.timestamp() * 1000)

    # Create logs client
    client = _create_logs_client(region_name=region_name, profile_name=profile_name)

    # Build request parameters
    kwargs = {
        'logGroupName': log_group_name,
        'startTime': start_time_ms,
        'endTime': end_time_ms,
        'interleaved': interleaved,
    }

    # Add optional parameters
    if filter_pattern is not None:
        kwargs['filterPattern'] = filter_pattern
    if log_stream_names is not None:
        kwargs['logStreamNames'] = log_stream_names

    # Create paginator and iterate through pages
    paginator = client.get_paginator('filter_log_events')
    page_iterator = paginator.paginate(
        PaginationConfig={'PageSize': 10000},
        **kwargs,
    )

    event_count = 0
    for page in page_iterator:
        for event in page.get('events', []):
            event_count += 1
            if progress_callback and event_count % 100 == 0:
                progress_callback(event_count, f'Fetched {event_count} events...')

            # Convert timestamps from epoch ms to datetime
            timestamp = _epoch_ms_to_datetime(event['timestamp'])
            ingestion_time = _epoch_ms_to_datetime(event['ingestionTime']) if 'ingestionTime' in event else None

            yield LogEvent(
                log_group=log_group_name,
                log_stream=event['logStreamName'],
                timestamp=timestamp,
                message=event['message'],
                event_id=event['eventId'],
                ingestion_time=ingestion_time,
            )
