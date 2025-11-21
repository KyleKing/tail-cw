"""Tests for async AWS CloudWatch Logs client."""

import asyncio
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tail_cw.aws.async_client import (
    MAX_CONCURRENT_STREAMS,
    fetch_log_events_async,
)
from tail_cw.aws.client import LogEvent


def _make_async_cw_event(
    log_stream_name: str = 'test-stream',
    timestamp: int = 1700000000000,
    message: str = 'Test log message',
    event_id: str = 'event-123',
    *,
    include_ingestion_time: bool = True,
) -> dict[str, Any]:
    """Create a mock CloudWatch event dict for testing.

    Args:
        log_stream_name: The log stream name.
        timestamp: Event timestamp in epoch milliseconds.
        message: The log message content.
        event_id: Unique event identifier.
        include_ingestion_time: Whether to include ingestionTime field.

    Returns:
        Dict matching CloudWatch API response structure.
    """
    event = {
        'logStreamName': log_stream_name,
        'timestamp': timestamp,
        'message': message,
        'eventId': event_id,
    }
    if include_ingestion_time:
        event['ingestionTime'] = timestamp + 1000  # 1 second later
    return event


@pytest.mark.asyncio
async def test_fetch_log_events_async_single_stream():
    """Test async fetch with single stream."""
    with patch('tail_cw.aws.async_client._create_async_logs_client') as mock_client_factory:
        # Setup mock client
        mock_client = AsyncMock()
        mock_paginator = MagicMock()  # Not async - get_paginator is synchronous

        # Mock the async context manager
        mock_client_factory.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_factory.return_value.__aexit__ = AsyncMock(return_value=None)

        # get_paginator should be a regular method, not async
        mock_client.get_paginator = MagicMock(return_value=mock_paginator)

        # Create mock page with events
        mock_page = {
            'events': [
                _make_async_cw_event(message='Event 1', event_id='1'),
                _make_async_cw_event(message='Event 2', event_id='2'),
            ],
        }

        # Mock async iteration
        async def mock_paginate(**kwargs):  # noqa: RUF029
            yield mock_page

        mock_paginator.paginate.return_value = mock_paginate()

        # Execute
        start = datetime(2025, 1, 1, tzinfo=UTC)
        end = datetime(2025, 1, 2, tzinfo=UTC)

        events = [event async for event in fetch_log_events_async(
            '/aws/lambda/test',
            start,
            end,
            log_stream_names=['single-stream'],
        )]

        # Verify
        assert len(events) == 2
        assert all(isinstance(e, LogEvent) for e in events)
        assert events[0].message == 'Event 1'
        assert events[1].message == 'Event 2'


@pytest.mark.asyncio
async def test_fetch_log_events_async_with_progress():
    """Test async fetch with progress callback."""
    progress_calls = []

    def progress_callback(count: int, message: str) -> None:
        progress_calls.append((count, message))

    with patch('tail_cw.aws.async_client._create_async_logs_client') as mock_client_factory:
        mock_client = AsyncMock()
        mock_paginator = MagicMock()  # Not async - get_paginator is synchronous

        mock_client_factory.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_factory.return_value.__aexit__ = AsyncMock(return_value=None)

        # get_paginator should be a regular method, not async
        mock_client.get_paginator = MagicMock(return_value=mock_paginator)

        # Create 150 events to trigger progress callback (every 100)
        events_data = [_make_async_cw_event(event_id=f'event-{i}') for i in range(150)]

        async def mock_paginate(**kwargs):  # noqa: RUF029
            yield {'events': events_data}

        mock_paginator.paginate.return_value = mock_paginate()

        start = datetime(2025, 1, 1, tzinfo=UTC)
        end = datetime(2025, 1, 2, tzinfo=UTC)

        [event async for event in fetch_log_events_async(
            '/aws/lambda/test',
            start,
            end,
            progress_callback=progress_callback,
        )]

        # Verify progress was called
        assert len(progress_calls) >= 1
        assert progress_calls[0][0] == 100  # First call at 100 events


@pytest.mark.asyncio
async def test_fetch_log_events_async_multiple_streams_concurrent():
    """Test async fetch with multiple streams uses concurrent fetching."""
    with (
        patch('tail_cw.aws.async_client._create_async_logs_client') as mock_client_factory,
        patch('tail_cw.aws.async_client._fetch_stream') as mock_fetch_stream,
    ):
        mock_client = AsyncMock()

        mock_client_factory.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_factory.return_value.__aexit__ = AsyncMock(return_value=None)

        # Mock _fetch_stream to return different events per stream
        async def fetch_stream_side_effect(client, *, log_group_name, stream_name, **kwargs):  # noqa: RUF029
            return [
                LogEvent(
                    log_group=log_group_name,
                    log_stream=stream_name,
                    timestamp=datetime(2025, 1, 1, tzinfo=UTC),
                    message=f'Event from {stream_name}',
                    event_id=f'{stream_name}-1',
                    ingestion_time=None,
                ),
            ]

        mock_fetch_stream.side_effect = fetch_stream_side_effect

        start = datetime(2025, 1, 1, tzinfo=UTC)
        end = datetime(2025, 1, 2, tzinfo=UTC)
        streams = ['stream1', 'stream2', 'stream3']

        events = [event async for event in fetch_log_events_async(
            '/aws/lambda/test',
            start,
            end,
            log_stream_names=streams,
        )]

        # Verify _fetch_stream was called for each stream
        assert mock_fetch_stream.call_count == 3

        # Verify events from all streams
        assert len(events) == 3
        stream_names = {e.log_stream for e in events}
        assert stream_names == {'stream1', 'stream2', 'stream3'}


@pytest.mark.asyncio
async def test_fetch_log_events_async_empty_results():
    """Test async fetch with no events."""
    with patch('tail_cw.aws.async_client._create_async_logs_client') as mock_client_factory:
        mock_client = AsyncMock()
        mock_paginator = MagicMock()  # Not async - get_paginator is synchronous

        mock_client_factory.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_factory.return_value.__aexit__ = AsyncMock(return_value=None)

        # get_paginator should be a regular method, not async
        mock_client.get_paginator = MagicMock(return_value=mock_paginator)

        # Empty page
        async def mock_paginate(**kwargs):  # noqa: RUF029
            yield {'events': []}

        mock_paginator.paginate.return_value = mock_paginate()

        start = datetime(2025, 1, 1, tzinfo=UTC)
        end = datetime(2025, 1, 2, tzinfo=UTC)

        events = [event async for event in fetch_log_events_async('/aws/lambda/test', start, end)]

        assert len(events) == 0


@pytest.mark.asyncio
async def test_max_concurrent_streams_limit():
    """Test that concurrent stream limit is respected."""
    # This test verifies the semaphore limiting
    assert MAX_CONCURRENT_STREAMS == 5

    with (
        patch('tail_cw.aws.async_client._create_async_logs_client') as mock_client_factory,
        patch('tail_cw.aws.async_client._fetch_stream') as mock_fetch_stream,
    ):
        mock_client = AsyncMock()

        mock_client_factory.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_factory.return_value.__aexit__ = AsyncMock(return_value=None)

        # Track concurrent calls
        concurrent_calls = []
        max_concurrent = 0

        async def fetch_stream_side_effect(client, *, log_group_name, stream_name, **kwargs):
            concurrent_calls.append(stream_name)
            nonlocal max_concurrent
            max_concurrent = max(max_concurrent, len(concurrent_calls))

            # Simulate some work
            await asyncio.sleep(0.01)

            concurrent_calls.remove(stream_name)

            return [
                LogEvent(
                    log_group=log_group_name,
                    log_stream=stream_name,
                    timestamp=datetime(2025, 1, 1, tzinfo=UTC),
                    message=f'Event from {stream_name}',
                    event_id=f'{stream_name}-1',
                    ingestion_time=None,
                ),
            ]

        mock_fetch_stream.side_effect = fetch_stream_side_effect

        start = datetime(2025, 1, 1, tzinfo=UTC)
        end = datetime(2025, 1, 2, tzinfo=UTC)

        # Try 10 streams (more than limit)
        streams = [f'stream{i}' for i in range(10)]

        events = [event async for event in fetch_log_events_async(
            '/aws/lambda/test',
            start,
            end,
            log_stream_names=streams,
        )]

        # Verify max concurrent calls never exceeded limit
        assert max_concurrent <= MAX_CONCURRENT_STREAMS
        assert len(events) == 10


@pytest.mark.asyncio
async def test_fetch_log_events_async_chronological_order():
    """Test that events from multiple streams are returned in chronological order."""
    with (
        patch('tail_cw.aws.async_client._create_async_logs_client') as mock_client_factory,
        patch('tail_cw.aws.async_client._fetch_stream') as mock_fetch_stream,
    ):
        mock_client = AsyncMock()

        mock_client_factory.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_factory.return_value.__aexit__ = AsyncMock(return_value=None)

        # Create events with different timestamps
        async def fetch_stream_side_effect(client, *, log_group_name, stream_name, **kwargs):  # noqa: RUF029
            # Stream1 has earlier events, stream2 has later events
            if stream_name == 'stream1':
                timestamp = datetime(2025, 1, 1, 10, 0, 0, tzinfo=UTC)
            else:
                timestamp = datetime(2025, 1, 1, 11, 0, 0, tzinfo=UTC)

            return [
                LogEvent(
                    log_group=log_group_name,
                    log_stream=stream_name,
                    timestamp=timestamp,
                    message=f'Event from {stream_name}',
                    event_id=f'{stream_name}-1',
                    ingestion_time=None,
                ),
            ]

        mock_fetch_stream.side_effect = fetch_stream_side_effect

        start = datetime(2025, 1, 1, tzinfo=UTC)
        end = datetime(2025, 1, 2, tzinfo=UTC)

        events = [event async for event in fetch_log_events_async(
            '/aws/lambda/test',
            start,
            end,
            log_stream_names=['stream1', 'stream2'],
        )]

        # Verify chronological order
        assert len(events) == 2
        assert events[0].log_stream == 'stream1'  # Earlier timestamp
        assert events[1].log_stream == 'stream2'  # Later timestamp
        assert events[0].timestamp < events[1].timestamp
