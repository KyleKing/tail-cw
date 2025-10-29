"""Tests for AWS CloudWatch Logs client."""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from botocore.exceptions import ClientError  # type: ignore[import-untyped]
from botocore.stub import ANY, Stubber  # type: ignore[import-untyped]

from tail_cw.aws.client import (
    LogEvent,
    _create_logs_client,
    _epoch_ms_to_datetime,
    fetch_log_events,
)


def _make_cw_event(
    log_stream_name: str = 'test-stream',
    timestamp: int = 1700000000000,
    message: str = 'Test log message',
    event_id: str = 'event-123',
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


def test_create_logs_client():
    """Test logs client creation with retry configuration."""
    with patch('tail_cw.aws.client.boto3.client') as mock_client:
        client = _create_logs_client()

        # Verify boto3.client was called correctly
        mock_client.assert_called_once()
        call_args = mock_client.call_args

        # Check service name
        assert call_args[0][0] == 'logs'

        # Check retry config
        config = call_args[1]['config']
        assert config.retries['max_attempts'] == 10
        assert config.retries['mode'] == 'standard'

        # Check region_name defaults to None
        assert call_args[1]['region_name'] is None

        assert client == mock_client.return_value


def test_create_logs_client_with_region():
    """Test logs client creation with explicit region."""
    with patch('tail_cw.aws.client.boto3.client') as mock_client:
        _create_logs_client(region_name='us-west-2')

        call_args = mock_client.call_args
        assert call_args[1]['region_name'] == 'us-west-2'


def test_fetch_log_events_single_page():
    """Test fetching logs with a single page of results."""
    # Create mock client and stub
    client = _create_logs_client()
    stub = Stubber(client)

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
    with patch('tail_cw.aws.client._create_logs_client', return_value=client):
        start = datetime.now(tz=UTC) - timedelta(hours=1)
        end = datetime.now(tz=UTC)

        results = list(fetch_log_events('/test/log-group', start, end))

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


def test_fetch_log_events_multiple_pages():
    """Test pagination with multiple pages."""
    client = _create_logs_client()
    stub = Stubber(client)

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

    with patch('tail_cw.aws.client._create_logs_client', return_value=client):
        start = datetime.now(tz=UTC) - timedelta(hours=1)
        end = datetime.now(tz=UTC)

        results = list(fetch_log_events('/test/log-group', start, end))

        # Should have all events from both pages
        assert len(results) == 3
        assert results[0].event_id == 'event-1'
        assert results[1].event_id == 'event-2'
        assert results[2].event_id == 'event-3'

    stub.deactivate()


def test_fetch_log_events_empty_page():
    """Test handling of empty pages."""
    client = _create_logs_client()
    stub = Stubber(client)

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

    with patch('tail_cw.aws.client._create_logs_client', return_value=client):
        start = datetime.now(tz=UTC) - timedelta(hours=1)
        end = datetime.now(tz=UTC)

        results = list(fetch_log_events('/test/log-group', start, end))

        # Should only have events from second page
        assert len(results) == 1
        assert results[0].event_id == 'event-1'

    stub.deactivate()


def test_fetch_log_events_with_filter_pattern():
    """Test with CloudWatch filter pattern."""
    client = _create_logs_client()
    stub = Stubber(client)

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

    with patch('tail_cw.aws.client._create_logs_client', return_value=client):
        start = datetime.now(tz=UTC) - timedelta(hours=1)
        end = datetime.now(tz=UTC)

        results = list(
            fetch_log_events(
                '/test/log-group',
                start,
                end,
                filter_pattern='[ERROR]',
            ),
        )

        assert len(results) == 1

    stub.deactivate()


def test_fetch_log_events_with_log_streams():
    """Test filtering by specific log streams."""
    client = _create_logs_client()
    stub = Stubber(client)

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

    with patch('tail_cw.aws.client._create_logs_client', return_value=client):
        start = datetime.now(tz=UTC) - timedelta(hours=1)
        end = datetime.now(tz=UTC)

        results = list(
            fetch_log_events(
                '/test/log-group',
                start,
                end,
                log_stream_names=['stream-1', 'stream-2'],
            ),
        )

        assert len(results) == 1

    stub.deactivate()


def test_fetch_log_events_without_ingestion_time():
    """Test handling events without ingestionTime field."""
    client = _create_logs_client()
    stub = Stubber(client)

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

    with patch('tail_cw.aws.client._create_logs_client', return_value=client):
        start = datetime.now(tz=UTC) - timedelta(hours=1)
        end = datetime.now(tz=UTC)

        results = list(fetch_log_events('/test/log-group', start, end))

        assert len(results) == 1
        assert results[0].ingestion_time is None

    stub.deactivate()


def test_fetch_log_events_client_error():
    """Test error handling for boto3 client errors."""
    client = _create_logs_client()
    stub = Stubber(client)

    # Add a client error
    stub.add_client_error(
        'filter_log_events',
        service_error_code='ThrottlingException',
        service_message='Rate exceeded',
    )

    stub.activate()

    with patch('tail_cw.aws.client._create_logs_client', return_value=client):
        start = datetime.now(tz=UTC) - timedelta(hours=1)
        end = datetime.now(tz=UTC)

        # Error should bubble up from the generator
        generator = fetch_log_events('/test/log-group', start, end)

        with pytest.raises(ClientError) as exc_info:
            list(generator)

        assert exc_info.value.response['Error']['Code'] == 'ThrottlingException'

    stub.deactivate()


def test_fetch_log_events_time_conversion():
    """Test datetime to epoch milliseconds conversion."""
    client = _create_logs_client()
    stub = Stubber(client)

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

    with patch('tail_cw.aws.client._create_logs_client', return_value=client):
        results = list(fetch_log_events('/test/log-group', start, end))
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


def test_fetch_log_events_interleaved_false():
    """Test with interleaved=False."""
    client = _create_logs_client()
    stub = Stubber(client)

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

    with patch('tail_cw.aws.client._create_logs_client', return_value=client):
        start = datetime.now(tz=UTC) - timedelta(hours=1)
        end = datetime.now(tz=UTC)

        results = list(
            fetch_log_events('/test/log-group', start, end, interleaved=False),
        )

        assert len(results) == 1

    stub.deactivate()


def test_fetch_log_events_large_time_range():
    """Test with very large time range."""
    client = _create_logs_client()
    stub = Stubber(client)

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

    with patch('tail_cw.aws.client._create_logs_client', return_value=client):
        results = list(fetch_log_events('/test/log-group', start, end))
        assert len(results) == 1

    stub.deactivate()


def test_fetch_log_events_microsecond_precision():
    """Test datetime with microsecond precision."""
    client = _create_logs_client()
    stub = Stubber(client)

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

    with patch('tail_cw.aws.client._create_logs_client', return_value=client):
        results = list(fetch_log_events('/test/log-group', start, end))
        assert len(results) == 1

    stub.deactivate()


def test_fetch_log_events_special_characters_in_log_group():
    """Test log group names with special characters."""
    client = _create_logs_client()
    stub = Stubber(client)

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

    with patch('tail_cw.aws.client._create_logs_client', return_value=client):
        start = datetime.now(tz=UTC) - timedelta(hours=1)
        end = datetime.now(tz=UTC)

        results = list(fetch_log_events(log_group, start, end))
        assert len(results) == 1
        assert results[0].log_group == log_group

    stub.deactivate()
