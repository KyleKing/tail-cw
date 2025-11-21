"""Tests for trace waterfall visualization."""

from datetime import UTC, datetime, timedelta

import pytest

from tail_cw.aws.client import LogEvent
from tail_cw.query.trace import TraceGroup, TraceSpan
from tail_cw.tui.trace_waterfall import TraceWaterfallView, render_waterfall_ascii


def _make_test_span(
    *,
    trace_id: str = 'trace-123',
    service_name: str = 'test-service',
    timestamp: datetime | None = None,
    duration_ms: float | None = 100.0,
    is_error: bool = False,
) -> TraceSpan:
    """Create a test TraceSpan."""
    if timestamp is None:
        timestamp = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)

    log_event = LogEvent(
        log_group='/aws/lambda/test',
        log_stream='test-stream',
        timestamp=timestamp,
        message='Test message',
        event_id='event-123',
        ingestion_time=None,
    )

    return TraceSpan(
        log_event=log_event,
        trace_id=trace_id,
        span_id='span-123',
        parent_span_id=None,
        service_name=service_name,
        duration_ms=duration_ms,
        is_error=is_error,
    )


def _make_test_trace(
    *,
    trace_id: str = 'trace-123',
    spans: list[TraceSpan] | None = None,
) -> TraceGroup:
    """Create a test TraceGroup."""
    if spans is None:
        spans = [_make_test_span(trace_id=trace_id)]

    start_time = min(span.log_event.timestamp for span in spans)
    end_time = max(span.log_event.timestamp for span in spans)
    duration_ms = (end_time - start_time).total_seconds() * 1000

    service_names = {span.service_name for span in spans}
    error_count = sum(1 for span in spans if span.is_error)

    return TraceGroup(
        trace_id=trace_id,
        spans=spans,
        start_time=start_time,
        end_time=end_time,
        duration_ms=duration_ms,
        service_names=service_names,
        error_count=error_count,
        span_count=len(spans),
    )


def test_waterfall_view_creation():
    """Test creating a waterfall view widget."""
    trace = _make_test_trace()
    view = TraceWaterfallView(trace)

    assert view._trace == trace
    assert isinstance(view._service_colors, dict)


def test_waterfall_service_colors():
    """Test service color assignment."""
    spans = [
        _make_test_span(service_name='service-a'),
        _make_test_span(service_name='service-b'),
        _make_test_span(service_name='service-c'),
    ]
    trace = _make_test_trace(spans=spans)
    view = TraceWaterfallView(trace)

    # Should assign colors to all services
    assert len(view._service_colors) == 3
    assert 'service-a' in view._service_colors
    assert 'service-b' in view._service_colors
    assert 'service-c' in view._service_colors

    # Colors should be from available palette
    colors = {'cyan', 'magenta', 'green', 'yellow', 'blue'}
    for color in view._service_colors.values():
        assert color in colors


def test_waterfall_renders_spans():
    """Test waterfall rendering of multiple spans."""
    base_time = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
    spans = [
        _make_test_span(
            service_name='api-gateway',
            timestamp=base_time,
            duration_ms=50.0,
        ),
        _make_test_span(
            service_name='auth-service',
            timestamp=base_time + timedelta(milliseconds=10),
            duration_ms=20.0,
        ),
        _make_test_span(
            service_name='database',
            timestamp=base_time + timedelta(milliseconds=30),
            duration_ms=15.0,
        ),
    ]
    trace = _make_test_trace(spans=spans)
    view = TraceWaterfallView(trace)

    result = view.render()

    # Should contain trace ID
    assert 'trace-123' in str(result)

    # Should contain service names
    assert 'api-gateway' in str(result)
    assert 'auth-service' in str(result)
    assert 'database' in str(result)

    # Should contain duration info
    assert 'Duration:' in str(result)
    assert 'Spans:' in str(result)


def test_waterfall_error_highlighting():
    """Test error span highlighting."""
    base_time = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
    spans = [
        _make_test_span(
            service_name='service-a',
            timestamp=base_time,
            duration_ms=50.0,
            is_error=False,
        ),
        _make_test_span(
            service_name='service-b',
            timestamp=base_time + timedelta(milliseconds=10),
            duration_ms=20.0,
            is_error=True,
        ),
    ]
    trace = _make_test_trace(spans=spans)
    view = TraceWaterfallView(trace)

    result = view.render()

    # Should indicate error count
    assert 'Errors: 1' in str(result)

    # Should contain error indicator
    assert '⚠' in str(result)


def test_waterfall_ascii_export():
    """Test ASCII waterfall export."""
    base_time = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
    spans = [
        _make_test_span(
            service_name='service-a',
            timestamp=base_time,
            duration_ms=100.0,
        ),
        _make_test_span(
            service_name='service-b',
            timestamp=base_time + timedelta(milliseconds=50),
            duration_ms=50.0,
        ),
    ]
    trace = _make_test_trace(spans=spans)

    result = render_waterfall_ascii(trace, width=80)

    # Should contain trace info
    assert 'Trace ID: trace-123' in result
    assert 'Duration:' in result

    # Should contain service names
    assert 'service-a' in result
    assert 'service-b' in result

    # Should have timeline bars
    assert '█' in result
    assert '|' in result


def test_waterfall_zero_duration_handling():
    """Test handling of spans with zero or None duration."""
    base_time = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
    spans = [
        _make_test_span(
            service_name='service-a',
            timestamp=base_time,
            duration_ms=None,
        ),
        _make_test_span(
            service_name='service-b',
            timestamp=base_time,
            duration_ms=0.0,
        ),
    ]
    trace = _make_test_trace(spans=spans)
    view = TraceWaterfallView(trace)

    result = view.render()

    # Should handle None/zero durations gracefully
    assert 'N/A' in str(result) or '0.0ms' in str(result)
    assert 'service-a' in str(result)
    assert 'service-b' in str(result)


def test_waterfall_single_span():
    """Test waterfall with single span."""
    span = _make_test_span()
    trace = _make_test_trace(spans=[span])
    view = TraceWaterfallView(trace)

    result = view.render()

    assert 'test-service' in str(result)
    assert 'Spans: 1' in str(result)
    assert 'Errors: 0' in str(result)
