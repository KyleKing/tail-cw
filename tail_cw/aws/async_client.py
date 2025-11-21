"""Async AWS CloudWatch Logs client with parallel stream fetching.

Provides async access to CloudWatch Logs via aioboto3 with support for
concurrent stream fetching to improve performance when querying multiple
log streams.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from datetime import datetime
from typing import Any

import aioboto3  # type: ignore[import-untyped]
from botocore.config import Config  # type: ignore[import-untyped]

from tail_cw.aws.client import LogEvent, _epoch_ms_to_datetime

# Maximum concurrent requests to CloudWatch API per region
MAX_CONCURRENT_STREAMS = 5

AsyncProgressCallback = Callable[[int, str], None]


def _create_async_logs_client(region_name: str | None = None) -> Any:
    """Create an async boto3 CloudWatch Logs client with retry configuration.

    Args:
        region_name: Optional AWS region name. If None, uses boto3's default
            region resolution (environment variables, config files, etc.).

    Returns:
        Configured aioboto3 CloudWatch Logs client context manager.
    """
    config = Config(
        retries={
            'max_attempts': 10,
            'mode': 'standard',
        },
    )
    session = aioboto3.Session()
    return session.client('logs', config=config, region_name=region_name)


async def fetch_log_events_async(
    log_group_name: str,
    start_time: datetime,
    end_time: datetime,
    *,
    filter_pattern: str | None = None,
    log_stream_names: list[str] | None = None,
    interleaved: bool = True,
    region_name: str | None = None,
    progress_callback: AsyncProgressCallback | None = None,
) -> AsyncIterator[LogEvent]:
    """Fetch CloudWatch Logs events asynchronously with efficient pagination.

    Streams log events from CloudWatch Logs without loading all events into memory.
    Uses aioboto3's async paginator to handle pagination automatically with retry
    logic for throttling errors.

    When multiple log streams are specified, fetches them concurrently (up to 5
    at a time) for improved performance.

    Args:
        log_group_name: CloudWatch log group name to query.
        start_time: Start of time range (inclusive). Must be timezone-aware.
        end_time: End of time range (inclusive). Must be timezone-aware.
        filter_pattern: Optional CloudWatch Logs filter pattern for server-side
            filtering. See AWS documentation for filter syntax.
        log_stream_names: Optional list of log stream names to filter. If None,
            searches all streams in the log group. If multiple streams provided,
            they are fetched concurrently.
        interleaved: Whether to interleave events from multiple streams
            chronologically. Default is True.
        region_name: Optional AWS region override. If None, uses boto3's default
            region resolution.
        progress_callback: Optional callable invoked every 100 events fetched.

    Yields:
        LogEvent instances for each log event in the time range.

    Example:
        >>> from datetime import datetime, timezone, timedelta
        >>> async def main():
        ...     start = datetime.now(tz=timezone.utc) - timedelta(hours=1)
        ...     end = datetime.now(tz=timezone.utc)
        ...     async for event in fetch_log_events_async('/aws/lambda/fn', start, end):
        ...         print(f"{event.timestamp}: {event.message}")
        >>> asyncio.run(main())

    Note:
        AWS credentials are resolved using boto3's default credential chain:
        environment variables, AWS profiles, IAM roles, etc. Errors from boto3
        (e.g., access denied, invalid log group) will bubble up to the caller.
        Throttling errors are handled automatically via retry configuration.
    """
    # Convert datetime to epoch milliseconds
    start_time_ms = int(start_time.timestamp() * 1000)
    end_time_ms = int(end_time.timestamp() * 1000)

    # If specific streams provided, fetch them concurrently
    if log_stream_names and len(log_stream_names) > 1:
        async for event in _fetch_multiple_streams_concurrent(
            log_group_name=log_group_name,
            stream_names=log_stream_names,
            start_time_ms=start_time_ms,
            end_time_ms=end_time_ms,
            filter_pattern=filter_pattern,
            region_name=region_name,
            progress_callback=progress_callback,
        ):
            yield event
    else:
        # Single stream or all streams - use standard approach
        async for event in _fetch_single_query(
            log_group_name=log_group_name,
            start_time_ms=start_time_ms,
            end_time_ms=end_time_ms,
            filter_pattern=filter_pattern,
            log_stream_names=log_stream_names,
            interleaved=interleaved,
            region_name=region_name,
            progress_callback=progress_callback,
        ):
            yield event


async def _fetch_single_query(
    log_group_name: str,
    start_time_ms: int,
    end_time_ms: int,
    *,
    filter_pattern: str | None = None,
    log_stream_names: list[str] | None = None,
    interleaved: bool = True,
    region_name: str | None = None,
    progress_callback: AsyncProgressCallback | None = None,
) -> AsyncIterator[LogEvent]:
    """Fetch logs using a single query (one or all streams).

    Args:
        log_group_name: CloudWatch log group name
        start_time_ms: Start time in epoch milliseconds
        end_time_ms: End time in epoch milliseconds
        filter_pattern: Optional filter pattern
        log_stream_names: Optional stream names
        interleaved: Whether to interleave events
        region_name: Optional region name
        progress_callback: Optional progress callback

    Yields:
        LogEvent instances
    """
    async with await _create_async_logs_client(region_name=region_name) as client:
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
        async for page in page_iterator:
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


async def _fetch_stream(
    client: Any,
    *,
    log_group_name: str,
    stream_name: str,
    start_time_ms: int,
    end_time_ms: int,
    filter_pattern: str | None,
) -> list[LogEvent]:
    """Fetch events from a single log stream.

    Args:
        client: Async CloudWatch Logs client
        log_group_name: Log group name
        stream_name: Stream name to fetch
        start_time_ms: Start time in epoch milliseconds
        end_time_ms: End time in epoch milliseconds
        filter_pattern: Optional filter pattern

    Returns:
        List of LogEvent instances from this stream
    """
    events = []

    kwargs = {
        'logGroupName': log_group_name,
        'logStreamNames': [stream_name],
        'startTime': start_time_ms,
        'endTime': end_time_ms,
        'interleaved': False,  # Single stream, no need to interleave
    }

    if filter_pattern is not None:
        kwargs['filterPattern'] = filter_pattern

    paginator = client.get_paginator('filter_log_events')
    page_iterator = paginator.paginate(
        PaginationConfig={'PageSize': 10000},
        **kwargs,
    )

    async for page in page_iterator:
        for event in page.get('events', []):
            timestamp = _epoch_ms_to_datetime(event['timestamp'])
            ingestion_time = _epoch_ms_to_datetime(event['ingestionTime']) if 'ingestionTime' in event else None

            events.append(
                LogEvent(
                    log_group=log_group_name,
                    log_stream=event['logStreamName'],
                    timestamp=timestamp,
                    message=event['message'],
                    event_id=event['eventId'],
                    ingestion_time=ingestion_time,
                ),
            )

    return events


async def _fetch_multiple_streams_concurrent(
    log_group_name: str,
    stream_names: list[str],
    start_time_ms: int,
    end_time_ms: int,
    *,
    filter_pattern: str | None = None,
    region_name: str | None = None,
    progress_callback: AsyncProgressCallback | None = None,
) -> AsyncIterator[LogEvent]:
    """Fetch logs from multiple streams concurrently.

    Limits concurrency to MAX_CONCURRENT_STREAMS to avoid overwhelming the
    CloudWatch API. Results are yielded in chronological order across all
    streams.

    Args:
        log_group_name: CloudWatch log group name
        stream_names: List of stream names to fetch
        start_time_ms: Start time in epoch milliseconds
        end_time_ms: End time in epoch milliseconds
        filter_pattern: Optional filter pattern
        region_name: Optional region name
        progress_callback: Optional progress callback

    Yields:
        LogEvent instances in chronological order
    """
    async with await _create_async_logs_client(region_name=region_name) as client:
        # Create semaphore to limit concurrency
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_STREAMS)

        async def fetch_with_semaphore(stream_name: str) -> list[LogEvent]:
            """Fetch stream with concurrency limit.

            Returns:
                List of LogEvent instances from the stream.
            """
            async with semaphore:
                return await _fetch_stream(
                    client,
                    log_group_name=log_group_name,
                    stream_name=stream_name,
                    start_time_ms=start_time_ms,
                    end_time_ms=end_time_ms,
                    filter_pattern=filter_pattern,
                )

        # Fetch all streams concurrently (with semaphore limiting)
        tasks = [fetch_with_semaphore(stream) for stream in stream_names]
        results = await asyncio.gather(*tasks)

        # Flatten results and sort by timestamp
        all_events = []
        for events in results:
            all_events.extend(events)

        all_events.sort(key=lambda e: e.timestamp)

        # Yield events with progress tracking
        for event_count, event in enumerate(all_events, start=1):
            if progress_callback and event_count % 100 == 0:
                progress_callback(event_count, f'Fetched {event_count} events...')
            yield event
