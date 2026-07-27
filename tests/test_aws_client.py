"""Tests for AWS CloudWatch Logs client."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from aiobotocore.stub import AioStubber  # type: ignore[import-untyped]
from botocore.exceptions import ClientError  # type: ignore[import-untyped]
from botocore.stub import ANY  # type: ignore[import-untyped]

from tail_cw.aws.client import (
    RETRIES,
    LogEvent,
    _epoch_ms_to_datetime,
    client_pool,
    fetch_log_events,
    retry_config,
)


async def _collect(events: AsyncIterator[LogEvent]) -> list[LogEvent]:
    return [event async for event in events]


@pytest.fixture
async def logs_client() -> AsyncIterator[Any]:
    """Open a CloudWatch Logs client for stubbing.

    Yields:
        The client, closed when the test ends.
    """
    async with client_pool(region_name='us-east-1') as pool:
        yield await pool.client('logs')


def _make_cw_event(
    log_stream_name: str = 'test-stream',
    timestamp: int = 1700000000000,
    message: str = 'Test log message',
    event_id: str = 'event-123',
    *,
    include_ingestion_time: bool = True,
):
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


def test_epoch_ms_to_datetime():
    """Test conversion from epoch milliseconds to datetime."""
    # Test basic conversion
    epoch_ms = 1700000000000  # Nov 14, 2023 22:13:20 UTC
    result = _epoch_ms_to_datetime(epoch_ms)

    assert isinstance(result, datetime)
    assert result.tzinfo == UTC
    assert result.year == 2023
    assert result.month == 11

    # Test conversion is reversible
    converted_back = int(result.timestamp() * 1000)
    assert converted_back == epoch_ms

    # Test edge case: epoch zero
    epoch_zero = _epoch_ms_to_datetime(0)
    assert epoch_zero.year == 1970
    assert epoch_zero.month == 1
    assert epoch_zero.day == 1

    # Test large timestamp
    large_timestamp = 2000000000000  # May 18, 2033
    result_large = _epoch_ms_to_datetime(large_timestamp)
    assert result_large.year == 2033


def test_retry_config_uses_standard_mode():
    """Throttling backoff comes from standard-mode retries."""
    assert RETRIES == {'max_attempts': 10, 'mode': 'standard'}


def test_each_client_gets_its_own_retry_config():
    assert retry_config() is not retry_config()


async def test_creating_a_client_does_not_rewrite_the_shared_retry_values():
    """Botocore rewrites the retries dict of the Config it is handed, so it must never get the shared one."""
    async with client_pool(region_name='us-east-1') as pool:
        await pool.client('logs')

    assert RETRIES == {'max_attempts': 10, 'mode': 'standard'}


async def test_client_pool_reuses_one_client_per_service():
    """A second request for a service returns the already-open client."""
    async with client_pool(region_name='us-west-2') as pool:
        first = await pool.client('logs')
        assert await pool.client('logs') is first
        assert await pool.client('cloudwatch') is not first


async def test_client_pool_passes_region_to_new_clients():
    """The pool's region reaches each client it creates."""
    async with client_pool(region_name='us-west-2') as pool:
        assert (await pool.client('logs')).meta.region_name == 'us-west-2'


async def test_fetch_log_events_single_page(logs_client: Any):
    """Test fetching logs with a single page of results."""
    # Create mock client and stub
    client = logs_client
    stub = AioStubber(client)

    # Prepare mock events
    events = [
        _make_cw_event(
            log_stream_name='stream-1',
            timestamp=1700000000000,
            message='First message',
            event_id='event-1',
        ),
        _make_cw_event(
            log_stream_name='stream-2',
            timestamp=1700000001000,
            message='Second message',
            event_id='event-2',
        ),
    ]

    # Add stub response
    stub.add_response(
        'filter_log_events',
        {
            'events': events,
            'searchedLogStreams': [],
        },
        expected_params={
            'logGroupName': '/test/log-group',
            'startTime': ANY,
            'endTime': ANY,
            'interleaved': True,
            'limit': 10000,
        },
    )

    stub.activate()

    # Patch the client creation to return our stubbed client
    start = datetime.now(tz=UTC) - timedelta(hours=1)
    end = datetime.now(tz=UTC)

    results = await _collect(fetch_log_events(client, '/test/log-group', start, end))

    assert len(results) == 2

    # Check first event
    assert results[0].log_group == '/test/log-group'
    assert results[0].log_stream == 'stream-1'
    assert results[0].message == 'First message'
    assert results[0].event_id == 'event-1'
    assert isinstance(results[0].timestamp, datetime)
    assert results[0].timestamp.tzinfo == UTC
    assert isinstance(results[0].ingestion_time, datetime)

    # Check second event
    assert results[1].log_stream == 'stream-2'
    assert results[1].message == 'Second message'

    stub.deactivate()


async def test_fetch_log_events_multiple_pages(logs_client: Any):
    """Test pagination with multiple pages."""
    client = logs_client
    stub = AioStubber(client)

    # First page with nextToken
    events_page1 = [
        _make_cw_event(event_id='event-1', message='Page 1 Event 1'),
        _make_cw_event(event_id='event-2', message='Page 1 Event 2'),
    ]

    stub.add_response(
        'filter_log_events',
        {
            'events': events_page1,
            'nextToken': 'token-123',
            'searchedLogStreams': [],
        },
        expected_params={
            'logGroupName': '/test/log-group',
            'startTime': ANY,
            'endTime': ANY,
            'interleaved': True,
            'limit': 10000,
        },
    )

    # Second page without nextToken (last page)
    events_page2 = [
        _make_cw_event(event_id='event-3', message='Page 2 Event 1'),
    ]

    stub.add_response(
        'filter_log_events',
        {
            'events': events_page2,
            'searchedLogStreams': [],
        },
        expected_params={
            'logGroupName': '/test/log-group',
            'startTime': ANY,
            'endTime': ANY,
            'interleaved': True,
            'limit': 10000,
            'nextToken': 'token-123',
        },
    )

    stub.activate()

    start = datetime.now(tz=UTC) - timedelta(hours=1)
    end = datetime.now(tz=UTC)

    results = await _collect(fetch_log_events(client, '/test/log-group', start, end))

    # Should have all events from both pages
    assert len(results) == 3
    assert results[0].event_id == 'event-1'
    assert results[1].event_id == 'event-2'
    assert results[2].event_id == 'event-3'

    stub.deactivate()


async def test_fetch_log_events_empty_page(logs_client: Any):
    """Test handling of empty pages."""
    client = logs_client
    stub = AioStubber(client)

    # First page: empty with nextToken
    stub.add_response(
        'filter_log_events',
        {
            'events': [],
            'nextToken': 'token-123',
            'searchedLogStreams': [],
        },
        expected_params={
            'logGroupName': '/test/log-group',
            'startTime': ANY,
            'endTime': ANY,
            'interleaved': True,
            'limit': 10000,
        },
    )

    # Second page: has events
    events = [_make_cw_event(event_id='event-1')]
    stub.add_response(
        'filter_log_events',
        {
            'events': events,
            'searchedLogStreams': [],
        },
        expected_params={
            'logGroupName': '/test/log-group',
            'startTime': ANY,
            'endTime': ANY,
            'interleaved': True,
            'limit': 10000,
            'nextToken': 'token-123',
        },
    )

    stub.activate()

    start = datetime.now(tz=UTC) - timedelta(hours=1)
    end = datetime.now(tz=UTC)

    results = await _collect(fetch_log_events(client, '/test/log-group', start, end))

    # Should only have events from second page
    assert len(results) == 1
    assert results[0].event_id == 'event-1'

    stub.deactivate()


async def test_fetch_log_events_with_progress_callback(logs_client: Any):
    """Progress callback should receive monotonic updates."""

    class DummyPaginator:
        def __init__(self, pages) -> None:
            self._pages = pages

        async def paginate(self, **_kwargs):
            for page in self._pages:
                yield page

    class DummyClient:
        def __init__(self, pages) -> None:
            self._pages = pages

        def get_paginator(self, name):
            assert name == 'filter_log_events'
            return DummyPaginator(self._pages)

    pages: list[dict[str, Any]] = []
    total_events = 250
    events = [
        _make_cw_event(
            event_id=f'event-{i}',
            timestamp=1700000000000 + i,
            message=f'Event {i}',
        )
        for i in range(total_events)
    ]
    pages.extend(({'events': events[:120]}, {'events': events[120:200]}, {'events': events[200:]}))

    calls = []

    def progress(current, status):
        calls.append((current, status))

    start = datetime.now(tz=UTC) - timedelta(hours=1)
    end = datetime.now(tz=UTC)

    client = DummyClient(pages)
    results = await _collect(
        fetch_log_events(
            client,
            '/test/log-group',
            start,
            end,
            progress_callback=progress,
        ),
    )

    assert len(results) == total_events
    assert calls, 'Expected progress callback to be invoked'
    counts = [count for count, _ in calls]
    assert counts == sorted(counts)
    assert counts[-1] >= 200
    for _, status in calls:
        assert status.startswith('Fetched')


async def test_fetch_log_events_progress_callback_frequency(logs_client: Any):
    """Progress callback should fire roughly every 100 events."""

    class DummyPaginator:
        def __init__(self, pages) -> None:
            self._pages = pages

        async def paginate(self, **_kwargs):
            for page in self._pages:
                yield page

    class DummyClient:
        def __init__(self, pages) -> None:
            self._pages = pages

        def get_paginator(self, name):
            assert name == 'filter_log_events'
            return DummyPaginator(self._pages)

    events = [
        _make_cw_event(
            event_id=f'event-{i}',
            timestamp=1700000000000 + i,
            message=f'Event {i}',
        )
        for i in range(250)
    ]
    pages = [{'events': events}]
    calls = []

    def progress(current, status):
        calls.append(current)

    start = datetime.now(tz=UTC) - timedelta(minutes=30)
    end = datetime.now(tz=UTC)

    client = DummyClient(pages)
    await _collect(
        fetch_log_events(
            client,
            '/test/log-group',
            start,
            end,
            progress_callback=progress,
        ),
    )

    assert calls == [100, 200]


async def test_fetch_log_events_without_progress_callback(logs_client: Any):
    """Ensure fetch works when no progress callback is supplied."""

    class DummyPaginator:
        def __init__(self, pages) -> None:
            self._pages = pages

        async def paginate(self, **_kwargs):
            for page in self._pages:
                yield page

    class DummyClient:
        def __init__(self, pages) -> None:
            self._pages = pages

        def get_paginator(self, name):
            assert name == 'filter_log_events'
            return DummyPaginator(self._pages)

    events = [
        _make_cw_event(
            event_id=f'event-{i}',
            timestamp=1700000000000 + i,
            message=f'Event {i}',
        )
        for i in range(50)
    ]

    client = DummyClient([{'events': events}])
    start = datetime.now(tz=UTC) - timedelta(minutes=5)
    end = datetime.now(tz=UTC)
    results = await _collect(fetch_log_events(client, '/test/log-group', start, end))

    assert len(results) == 50


async def test_fetch_log_events_with_filter_pattern(logs_client: Any):
    """Test with CloudWatch filter pattern."""
    client = logs_client
    stub = AioStubber(client)

    events = [_make_cw_event()]

    stub.add_response(
        'filter_log_events',
        {
            'events': events,
            'searchedLogStreams': [],
        },
        expected_params={
            'logGroupName': '/test/log-group',
            'startTime': ANY,
            'endTime': ANY,
            'interleaved': True,
            'limit': 10000,
            'filterPattern': '[ERROR]',
        },
    )

    stub.activate()

    start = datetime.now(tz=UTC) - timedelta(hours=1)
    end = datetime.now(tz=UTC)

    results = await _collect(
        fetch_log_events(
            client,
            '/test/log-group',
            start,
            end,
            filter_pattern='[ERROR]',
        ),
    )

    assert len(results) == 1

    stub.deactivate()


async def test_fetch_log_events_with_log_streams(logs_client: Any):
    """Test filtering by specific log streams."""
    client = logs_client
    stub = AioStubber(client)

    events = [_make_cw_event()]

    stub.add_response(
        'filter_log_events',
        {
            'events': events,
            'searchedLogStreams': [],
        },
        expected_params={
            'logGroupName': '/test/log-group',
            'startTime': ANY,
            'endTime': ANY,
            'interleaved': True,
            'limit': 10000,
            'logStreamNames': ['stream-1', 'stream-2'],
        },
    )

    stub.activate()

    start = datetime.now(tz=UTC) - timedelta(hours=1)
    end = datetime.now(tz=UTC)

    results = await _collect(
        fetch_log_events(
            client,
            '/test/log-group',
            start,
            end,
            log_stream_names=['stream-1', 'stream-2'],
        ),
    )

    assert len(results) == 1

    stub.deactivate()


async def test_fetch_log_events_without_ingestion_time(logs_client: Any):
    """Test handling events without ingestionTime field."""
    client = logs_client
    stub = AioStubber(client)

    # Create event without ingestionTime
    events = [_make_cw_event(include_ingestion_time=False)]

    stub.add_response(
        'filter_log_events',
        {
            'events': events,
            'searchedLogStreams': [],
        },
        expected_params={
            'logGroupName': '/test/log-group',
            'startTime': ANY,
            'endTime': ANY,
            'interleaved': True,
            'limit': 10000,
        },
    )

    stub.activate()

    start = datetime.now(tz=UTC) - timedelta(hours=1)
    end = datetime.now(tz=UTC)

    results = await _collect(fetch_log_events(client, '/test/log-group', start, end))

    assert len(results) == 1
    assert results[0].ingestion_time is None

    stub.deactivate()


async def test_fetch_log_events_client_error(logs_client: Any):
    """Test error handling for boto3 client errors."""
    client = logs_client
    stub = AioStubber(client)

    # Add a client error
    stub.add_client_error(
        'filter_log_events',
        service_error_code='ThrottlingException',
        service_message='Rate exceeded',
    )

    stub.activate()

    start = datetime.now(tz=UTC) - timedelta(hours=1)
    end = datetime.now(tz=UTC)

    # Error should bubble up from the generator
    with pytest.raises(ClientError) as exc_info:
        await _collect(fetch_log_events(client, '/test/log-group', start, end))

    assert exc_info.value.response['Error']['Code'] == 'ThrottlingException'

    stub.deactivate()


async def test_fetch_log_events_time_conversion(logs_client: Any):
    """Test datetime to epoch milliseconds conversion."""
    client = logs_client
    stub = AioStubber(client)

    # Create specific datetime with known epoch values
    start = datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)
    end = datetime(2023, 11, 14, 23, 13, 20, tzinfo=UTC)

    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)

    events = [_make_cw_event()]

    stub.add_response(
        'filter_log_events',
        {
            'events': events,
            'searchedLogStreams': [],
        },
        expected_params={
            'logGroupName': '/test/log-group',
            'startTime': start_ms,
            'endTime': end_ms,
            'interleaved': True,
            'limit': 10000,
        },
    )

    stub.activate()

    results = await _collect(fetch_log_events(client, '/test/log-group', start, end))
    assert len(results) == 1

    stub.deactivate()


def test_log_event_dataclass_immutability():
    """Test that LogEvent is immutable."""
    event = LogEvent(
        log_group='/test/group',
        log_stream='test-stream',
        timestamp=datetime.now(tz=UTC),
        message='test',
        event_id='123',
        ingestion_time=None,
    )

    # Should not be able to modify frozen dataclass
    with pytest.raises(AttributeError):
        event.message = 'modified'  # type: ignore[misc]


async def test_fetch_log_events_interleaved_false(logs_client: Any):
    """Test with interleaved=False."""
    client = logs_client
    stub = AioStubber(client)

    events = [_make_cw_event()]

    stub.add_response(
        'filter_log_events',
        {
            'events': events,
            'searchedLogStreams': [],
        },
        expected_params={
            'logGroupName': '/test/log-group',
            'startTime': ANY,
            'endTime': ANY,
            'interleaved': False,
            'limit': 10000,
        },
    )

    stub.activate()

    start = datetime.now(tz=UTC) - timedelta(hours=1)
    end = datetime.now(tz=UTC)

    results = await _collect(
        fetch_log_events(client, '/test/log-group', start, end, interleaved=False),
    )

    assert len(results) == 1

    stub.deactivate()


async def test_fetch_log_events_large_time_range(logs_client: Any):
    """Test with very large time range."""
    client = logs_client
    stub = AioStubber(client)

    # 30 days time range
    start = datetime.now(tz=UTC) - timedelta(days=30)
    end = datetime.now(tz=UTC)

    events = [_make_cw_event()]

    stub.add_response(
        'filter_log_events',
        {
            'events': events,
            'searchedLogStreams': [],
        },
        expected_params={
            'logGroupName': '/test/log-group',
            'startTime': ANY,
            'endTime': ANY,
            'interleaved': True,
            'limit': 10000,
        },
    )

    stub.activate()

    results = await _collect(fetch_log_events(client, '/test/log-group', start, end))
    assert len(results) == 1

    stub.deactivate()


async def test_fetch_log_events_microsecond_precision(logs_client: Any):
    """Test datetime with microsecond precision."""
    client = logs_client
    stub = AioStubber(client)

    # Create datetime with microseconds
    start = datetime(2023, 11, 14, 22, 13, 20, 123456, tzinfo=UTC)
    end = datetime(2023, 11, 14, 23, 13, 20, 654321, tzinfo=UTC)

    events = [_make_cw_event()]

    stub.add_response(
        'filter_log_events',
        {
            'events': events,
            'searchedLogStreams': [],
        },
        expected_params={
            'logGroupName': '/test/log-group',
            'startTime': ANY,
            'endTime': ANY,
            'interleaved': True,
            'limit': 10000,
        },
    )

    stub.activate()

    results = await _collect(fetch_log_events(client, '/test/log-group', start, end))
    assert len(results) == 1

    stub.deactivate()


async def test_fetch_log_events_special_characters_in_log_group(logs_client: Any):
    """Test log group names with special characters."""
    client = logs_client
    stub = AioStubber(client)

    log_group = '/aws/lambda/my-function-name_v2'
    events = [_make_cw_event()]

    stub.add_response(
        'filter_log_events',
        {
            'events': events,
            'searchedLogStreams': [],
        },
        expected_params={
            'logGroupName': log_group,
            'startTime': ANY,
            'endTime': ANY,
            'interleaved': True,
            'limit': 10000,
        },
    )

    stub.activate()

    start = datetime.now(tz=UTC) - timedelta(hours=1)
    end = datetime.now(tz=UTC)

    results = await _collect(fetch_log_events(client, log_group, start, end))
    assert len(results) == 1
    assert results[0].log_group == log_group

    stub.deactivate()
