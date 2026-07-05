"""Tests for trace extraction and grouping functionality."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tail_cw.aws.client import LogEvent
from tail_cw.cache.storage import write_log_events_to_parquet
from tail_cw.query.trace import (
    DEFAULT_TRACE_ID_FIELDS,
    TraceGroup,
    TraceSpan,
    create_trace_groups,
    extract_service_name,
    extract_span_metadata,
    extract_trace_id_from_event,
    find_traces_with_errors,
    format_trace_duration,
    group_events_by_trace,
    is_error_event,
    log_event_to_trace_span,
    query_traces_from_parquet,
)


def _make_test_event_with_trace(
    trace_id: str,
    *,
    service_name: str = 'test-service',
    is_error: bool = False,
    span_id: str | None = None,
    parent_span_id: str | None = None,
    timestamp: datetime | None = None,
) -> LogEvent:
    """Create LogEvent with trace ID.

    Args:
        trace_id: Trace ID to embed
        service_name: Service name
        is_error: Whether event is an error
        span_id: Optional span ID
        parent_span_id: Optional parent span ID
        timestamp: Optional timestamp

    Returns:
        LogEvent with JSONL message containing trace metadata
    """
    message_data = {
        'trace_id': trace_id,
        'service_name': service_name,
        'level': 'ERROR' if is_error else 'INFO',
    }

    if span_id:
        message_data['span_id'] = span_id

    if parent_span_id:
        message_data['parent_span_id'] = parent_span_id

    message = json.dumps(message_data)

    return LogEvent(
        message=message,
        timestamp=timestamp or datetime.now(UTC),
        ingestion_time=datetime.now(UTC),
        log_stream='test-stream',
        log_group='/aws/lambda/test-function',
        event_id='test-event-id',
    )


def test_extract_trace_id_from_event_found():
    event = _make_test_event_with_trace('trace-123')
    trace_id = extract_trace_id_from_event(event)
    assert trace_id == 'trace-123'


def test_extract_trace_id_from_event_not_found():
    event = LogEvent(
        message='Plain log message',
        timestamp=datetime.now(UTC),
        ingestion_time=datetime.now(UTC),
        log_stream='test-stream',
        log_group='test-group',
        event_id='test-event-id',
    )
    trace_id = extract_trace_id_from_event(event)
    assert trace_id is None


def test_extract_trace_id_from_event_multiple_fields():
    # Test different field name variations
    for field_name in ['trace_id', 'traceId', 'x-trace-id']:
        message = json.dumps({field_name: 'test-trace'})
        event = LogEvent(
            message=message,
            timestamp=datetime.now(UTC),
            ingestion_time=datetime.now(UTC),
            log_stream='test-stream',
            log_group='test-group',
            event_id='test-event-id',
        )
        trace_id = extract_trace_id_from_event(event)
        assert trace_id == 'test-trace'


def test_extract_trace_id_from_event_nested():
    message = json.dumps({'context': {'trace_id': 'nested-trace'}})
    event = LogEvent(
        message=message,
        timestamp=datetime.now(UTC),
        ingestion_time=datetime.now(UTC),
        log_stream='test-stream',
        log_group='test-group',
        event_id='test-event-id',
    )
    # Add nested field to search list
    trace_id = extract_trace_id_from_event(
        event,
        trace_id_fields=[*DEFAULT_TRACE_ID_FIELDS, 'context.trace_id'],
    )
    assert trace_id == 'nested-trace'


def test_extract_service_name_from_json():
    event = _make_test_event_with_trace('trace-123', service_name='my-service')
    service_name = extract_service_name(event)
    assert service_name == 'my-service'


def test_extract_service_name_from_log_group():
    event = LogEvent(
        message='Plain message',
        timestamp=datetime.now(UTC),
        ingestion_time=datetime.now(UTC),
        log_stream='test-stream',
        log_group='/aws/lambda/my-function',
        event_id='test-event-id',
    )
    service_name = extract_service_name(event)
    assert service_name == 'my-function'


def test_is_error_event_true():
    # Test with ERROR level
    event = _make_test_event_with_trace('trace-123', is_error=True)
    assert is_error_event(event) is True

    # Test with ERROR keyword in message
    event = LogEvent(
        message='ERROR: Something failed',
        timestamp=datetime.now(UTC),
        ingestion_time=datetime.now(UTC),
        log_stream='test-stream',
        log_group='test-group',
        event_id='test-event-id',
    )
    assert is_error_event(event) is True

    # Test with status code 500
    message = json.dumps({'status': 500, 'message': 'Internal error'})
    event = LogEvent(
        message=message,
        timestamp=datetime.now(UTC),
        ingestion_time=datetime.now(UTC),
        log_stream='test-stream',
        log_group='test-group',
        event_id='test-event-id',
    )
    assert is_error_event(event) is True


def test_is_error_event_false():
    event = _make_test_event_with_trace('trace-123', is_error=False)
    assert is_error_event(event) is False

    # Test normal message
    event = LogEvent(
        message='INFO: Processing request',
        timestamp=datetime.now(UTC),
        ingestion_time=datetime.now(UTC),
        log_stream='test-stream',
        log_group='test-group',
        event_id='test-event-id',
    )
    assert is_error_event(event) is False


def test_extract_span_metadata():
    message = json.dumps(
        {
            'span_id': 'span-123',
            'parent_span_id': 'parent-456',
            'duration_ms': 123.45,
        },
    )
    event = LogEvent(
        message=message,
        timestamp=datetime.now(UTC),
        ingestion_time=datetime.now(UTC),
        log_stream='test-stream',
        log_group='test-group',
        event_id='test-event-id',
    )
    metadata = extract_span_metadata(event)
    assert metadata['span_id'] == 'span-123'
    assert metadata['parent_span_id'] == 'parent-456'
    assert metadata['duration_ms'] == pytest.approx(123.45)


def test_log_event_to_trace_span():
    event = _make_test_event_with_trace(
        'trace-123',
        service_name='my-service',
        is_error=False,
        span_id='span-456',
    )
    span = log_event_to_trace_span(event, 'trace-123')

    assert span.trace_id == 'trace-123'
    assert span.service_name == 'my-service'
    assert span.is_error is False
    assert span.span_id == 'span-456'
    assert span.log_event == event


def test_group_events_by_trace():
    # Create events with 2 different trace IDs
    events = [
        _make_test_event_with_trace('trace-1'),
        _make_test_event_with_trace('trace-1'),
        _make_test_event_with_trace('trace-1'),
        _make_test_event_with_trace('trace-2'),
        _make_test_event_with_trace('trace-2'),
    ]

    grouped = group_events_by_trace(events)

    assert len(grouped) == 2
    assert len(grouped['trace-1']) == 3
    assert len(grouped['trace-2']) == 2


def test_group_events_by_trace_no_trace_ids():
    events = [
        LogEvent(
            message='Plain message',
            timestamp=datetime.now(UTC),
            ingestion_time=datetime.now(UTC),
            log_stream='test-stream',
            log_group='test-group',
            event_id='test-event-id',
        )
        for _ in range(3)
    ]

    grouped = group_events_by_trace(events)
    assert len(grouped) == 0


def test_create_trace_groups():
    # Create grouped spans
    base_time = datetime.now(UTC)
    events = [
        _make_test_event_with_trace('trace-1', timestamp=base_time),
        _make_test_event_with_trace('trace-1', timestamp=base_time, is_error=True),
        _make_test_event_with_trace('trace-2', timestamp=base_time),
    ]

    grouped = group_events_by_trace(events)
    trace_groups = create_trace_groups(grouped)

    assert len(trace_groups) == 2
    assert trace_groups[0].span_count in {2, 1}
    assert any(tg.error_count == 1 for tg in trace_groups)


def test_create_trace_groups_chronological_sorting():
    base_time = datetime.now(UTC)
    # Create spans out of order
    events = [
        _make_test_event_with_trace('trace-1', timestamp=base_time),
        _make_test_event_with_trace('trace-1', timestamp=base_time - timedelta(seconds=1)),
        _make_test_event_with_trace('trace-1', timestamp=base_time + timedelta(seconds=1)),
    ]

    grouped = group_events_by_trace(events)
    trace_groups = create_trace_groups(grouped)

    # Verify spans are sorted
    trace_group = trace_groups[0]
    timestamps = [span.log_event.timestamp for span in trace_group.spans]
    assert timestamps == sorted(timestamps)


def test_create_trace_groups_duration_calculation():
    base_time = datetime.now(UTC)
    events = [
        _make_test_event_with_trace('trace-1', timestamp=base_time),
        _make_test_event_with_trace('trace-1', timestamp=base_time + timedelta(seconds=5)),
    ]

    grouped = group_events_by_trace(events)
    trace_groups = create_trace_groups(grouped)

    trace_group = trace_groups[0]
    # Duration should be approximately 5000ms
    assert 4900 <= trace_group.duration_ms <= 5100


def test_query_traces_from_parquet_empty(tmp_path: Path):
    # Create Parquet with events without trace IDs
    events = [
        LogEvent(
            message='Plain message',
            timestamp=datetime.now(UTC),
            ingestion_time=datetime.now(UTC),
            log_stream='test-stream',
            log_group='test-group',
            event_id='test-event-id',
        )
        for _ in range(3)
    ]

    parquet_path = tmp_path / 'logs.parquet'
    write_log_events_to_parquet(events, parquet_path)

    trace_groups = query_traces_from_parquet(parquet_path)
    assert len(trace_groups) == 0


def test_query_traces_from_parquet_all(tmp_path: Path):
    # Create Parquet with multiple traces
    events = [
        _make_test_event_with_trace('trace-1'),
        _make_test_event_with_trace('trace-1'),
        _make_test_event_with_trace('trace-2'),
        _make_test_event_with_trace('trace-2'),
        _make_test_event_with_trace('trace-3'),
    ]

    parquet_path = tmp_path / 'logs.parquet'
    write_log_events_to_parquet(events, parquet_path)

    trace_groups = query_traces_from_parquet(parquet_path)
    assert len(trace_groups) == 3


def test_query_traces_from_parquet_specific_trace(tmp_path: Path):
    # Create Parquet with multiple traces
    events = [
        _make_test_event_with_trace('trace-1'),
        _make_test_event_with_trace('trace-1'),
        _make_test_event_with_trace('trace-2'),
        _make_test_event_with_trace('trace-3'),
    ]

    parquet_path = tmp_path / 'logs.parquet'
    write_log_events_to_parquet(events, parquet_path)

    trace_groups = query_traces_from_parquet(parquet_path, trace_id='trace-1')
    assert len(trace_groups) == 1
    assert trace_groups[0].trace_id == 'trace-1'


def test_query_traces_from_parquet_limit(tmp_path: Path):
    # Create Parquet with many traces
    events = [_make_test_event_with_trace(f'trace-{i}') for i in range(10)]

    parquet_path = tmp_path / 'logs.parquet'
    write_log_events_to_parquet(events, parquet_path)

    trace_groups = query_traces_from_parquet(parquet_path, limit=5)
    assert len(trace_groups) <= 5


def test_find_traces_with_errors(tmp_path: Path):
    # Create Parquet with some error events
    events = [
        _make_test_event_with_trace('trace-1', is_error=True),
        _make_test_event_with_trace('trace-1', is_error=False),
        _make_test_event_with_trace('trace-2', is_error=False),
        _make_test_event_with_trace('trace-3', is_error=True),
        _make_test_event_with_trace('trace-3', is_error=True),
    ]

    parquet_path = tmp_path / 'logs.parquet'
    write_log_events_to_parquet(events, parquet_path)

    error_traces = find_traces_with_errors(parquet_path)

    # Should only return traces with errors
    assert len(error_traces) == 2
    assert all(tg.error_count > 0 for tg in error_traces)

    # Should be sorted by error_count descending
    if len(error_traces) >= 2:
        assert error_traces[0].error_count >= error_traces[1].error_count


def test_format_trace_duration_milliseconds():
    result = format_trace_duration(123)
    assert result == '123ms'


def test_format_trace_duration_seconds():
    result = format_trace_duration(1234)
    assert result == '1.23s'


def test_format_trace_duration_minutes():
    result = format_trace_duration(65000)
    assert result == '1m 5s'


def test_trace_span_dataclass():
    event = _make_test_event_with_trace('trace-123')
    span = TraceSpan(
        log_event=event,
        trace_id='trace-123',
        span_id='span-456',
        parent_span_id=None,
        service_name='my-service',
        duration_ms=100.0,
        is_error=False,
    )

    assert span.trace_id == 'trace-123'
    assert span.span_id == 'span-456'
    assert span.service_name == 'my-service'
    assert span.duration_ms == pytest.approx(100.0)
    assert span.is_error is False


def test_trace_group_dataclass():
    base_time = datetime.now(UTC)
    event = _make_test_event_with_trace('trace-123', timestamp=base_time)
    span = log_event_to_trace_span(event, 'trace-123')

    trace_group = TraceGroup(
        trace_id='trace-123',
        spans=[span],
        start_time=base_time,
        end_time=base_time,
        duration_ms=0.0,
        service_names={'my-service'},
        error_count=0,
        span_count=1,
    )

    assert trace_group.trace_id == 'trace-123'
    assert trace_group.span_count == 1
    assert 'my-service' in trace_group.service_names


def test_extract_trace_id_malformed_json():
    event = LogEvent(
        message='{invalid json',
        timestamp=datetime.now(UTC),
        ingestion_time=datetime.now(UTC),
        log_stream='test-stream',
        log_group='test-group',
        event_id='test-event-id',
    )
    trace_id = extract_trace_id_from_event(event)
    assert trace_id is None


def test_extract_service_name_unicode():
    message = json.dumps({'service_name': 'my-service-🚀'})
    event = LogEvent(
        message=message,
        timestamp=datetime.now(UTC),
        ingestion_time=datetime.now(UTC),
        log_stream='test-stream',
        log_group='test-group',
        event_id='test-event-id',
    )
    service_name = extract_service_name(event)
    assert service_name == 'my-service-🚀'
