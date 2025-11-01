"""Unit tests for the trace viewer TUI screen."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from textual.widgets import Input, Label, Tree

from tail_cw.aws.client import LogEvent
from tail_cw.query.trace import TraceGroup, log_event_to_trace_span
from tail_cw.tui.app import LogTailApp
from tail_cw.tui.record_detail import RecordDetailScreen
from tail_cw.tui.trace_viewer import TraceViewerScreen


def _make_test_event(
    trace_id: str,
    *,
    service_name: str = 'test-service',
    message: str = 'Test message',
    is_error: bool = False,
    timestamp: datetime | None = None,
) -> LogEvent:
    """Create test LogEvent with trace metadata.

    Returns:
        LogEvent populated with basic trace fields for testing.
    """
    message_data = {
        'trace_id': trace_id,
        'service_name': service_name,
        'level': 'ERROR' if is_error else 'INFO',
    }

    return LogEvent(
        message=json.dumps(message_data),
        timestamp=timestamp or datetime.now(UTC),
        ingestion_time=datetime.now(UTC),
        log_stream='test-stream',
        log_group='/aws/lambda/test-function',
        event_id='test-event-id',
    )


def _make_test_trace_group(
    trace_id: str = 'test-trace-123',
    span_count: int = 3,
    error_count: int = 0,
) -> TraceGroup:
    """Create test TraceGroup.

    Args:
        trace_id: Trace ID
        span_count: Number of spans to create
        error_count: Number of error spans

    Returns:
        TraceGroup with test data
    """
    base_time = datetime.now(UTC)
    spans = []

    for i in range(span_count):
        is_error = i < error_count
        event = _make_test_event(
            trace_id=trace_id,
            service_name=f'service-{i % 2}',
            message=f'Span {i}',
            is_error=is_error,
            timestamp=base_time + timedelta(seconds=i),
        )
        span = log_event_to_trace_span(event, trace_id)
        spans.append(span)

    # Sort chronologically
    spans.sort(key=lambda s: s.log_event.timestamp)

    return TraceGroup(
        trace_id=trace_id,
        spans=spans,
        start_time=spans[0].log_event.timestamp,
        end_time=spans[-1].log_event.timestamp,
        duration_ms=(spans[-1].log_event.timestamp - spans[0].log_event.timestamp).total_seconds() * 1000,
        service_names={span.service_name for span in spans},
        error_count=error_count,
        span_count=span_count,
    )


def test_trace_viewer_screen_initialization():
    trace_groups = [_make_test_trace_group()]
    screen = TraceViewerScreen(trace_groups)

    assert screen._trace_groups == trace_groups
    assert screen._all_trace_groups == trace_groups


@pytest.mark.asyncio
async def test_trace_viewer_screen_compose_structure():
    trace_groups = [_make_test_trace_group()]
    screen = TraceViewerScreen(trace_groups)
    app = LogTailApp()

    async with app.run_test() as pilot:
        app.push_screen(screen)
        await pilot.pause()

        # Query for widgets
        tree = app.screen.query_one('#trace_tree', Tree)
        search_input = app.screen.query_one('#trace_search', Input)
        status = app.screen.query_one('#trace_status', Label)

        assert tree is not None
        assert search_input is not None
        assert status is not None


@pytest.mark.asyncio
async def test_trace_viewer_tree_population():
    trace_groups = [
        _make_test_trace_group('trace-1', span_count=3),
        _make_test_trace_group('trace-2', span_count=2),
    ]
    screen = TraceViewerScreen(trace_groups)
    app = LogTailApp()

    async with app.run_test() as pilot:
        app.push_screen(screen)
        await pilot.pause()

        tree = app.screen.query_one('#trace_tree', Tree)

        # Tree should have 2 root nodes (traces)
        assert len(tree.root.children) == 2


@pytest.mark.asyncio
async def test_trace_viewer_tree_structure():
    # Create trace with 2 services, 3 spans each
    trace_id = 'test-trace'
    base_time = datetime.now(UTC)
    spans = []

    for service_idx in range(2):
        for span_idx in range(3):
            event = _make_test_event(
                trace_id=trace_id,
                service_name=f'service-{service_idx}',
                message=f'Span {span_idx}',
                timestamp=base_time + timedelta(seconds=service_idx * 3 + span_idx),
            )
            span = log_event_to_trace_span(event, trace_id)
            spans.append(span)

    trace_group = TraceGroup(
        trace_id=trace_id,
        spans=sorted(spans, key=lambda s: s.log_event.timestamp),
        start_time=base_time,
        end_time=base_time + timedelta(seconds=6),
        duration_ms=6000.0,
        service_names={'service-0', 'service-1'},
        error_count=0,
        span_count=6,
    )

    screen = TraceViewerScreen([trace_group])
    app = LogTailApp()

    async with app.run_test() as pilot:
        app.push_screen(screen)
        await pilot.pause()

        tree = app.screen.query_one('#trace_tree', Tree)

        # Should have 1 trace root
        assert len(tree.root.children) == 1

        # Trace root should have 2 service children
        trace_node = tree.root.children[0]
        assert len(trace_node.children) == 2


@pytest.mark.asyncio
async def test_trace_viewer_error_highlighting():
    trace_group = _make_test_trace_group('trace-1', span_count=3, error_count=1)
    screen = TraceViewerScreen([trace_group])
    app = LogTailApp()

    async with app.run_test() as pilot:
        app.push_screen(screen)
        await pilot.pause()

        # Tree is populated with error spans
        # Error styling is applied via Rich Text objects
        # This test verifies the tree is built without errors
        tree = app.screen.query_one('#trace_tree', Tree)
        assert len(tree.root.children) > 0


@pytest.mark.asyncio
async def test_trace_viewer_expand_collapse():
    trace_group = _make_test_trace_group('trace-1', span_count=5)
    screen = TraceViewerScreen([trace_group])
    app = LogTailApp()

    async with app.run_test() as pilot:
        app.push_screen(screen)
        await pilot.pause()

        # Press 'e' to expand all
        await pilot.press('e')
        await pilot.pause()

        # Press 'c' to collapse all
        await pilot.press('c')
        await pilot.pause()

        # Should not raise errors
        status = app.screen.query_one('#trace_status', Label)
        assert 'Collapsed all nodes' in str(status.render())


@pytest.mark.asyncio
async def test_trace_viewer_keyboard_navigation():
    trace_group = _make_test_trace_group('trace-1', span_count=3)
    screen = TraceViewerScreen([trace_group])
    app = LogTailApp()

    async with app.run_test() as pilot:
        app.push_screen(screen)
        await pilot.pause()

        tree = app.screen.query_one('#trace_tree', Tree)

        # Press down arrow to navigate
        await pilot.press('down')
        await pilot.pause()

        # Should not raise errors
        assert tree.cursor_node is not None


@pytest.mark.asyncio
async def test_trace_viewer_search():
    trace_groups = [
        _make_test_trace_group('trace-aaa', span_count=2),
        _make_test_trace_group('trace-bbb', span_count=2),
    ]
    screen = TraceViewerScreen(trace_groups)
    app = LogTailApp()

    async with app.run_test() as pilot:
        app.push_screen(screen)
        await pilot.pause()

        search_input = app.screen.query_one('#trace_search', Input)

        # Type search query
        search_input.value = 'aaa'
        await pilot.pause()

        tree = app.screen.query_one('#trace_tree', Tree)

        # Tree should be filtered to one trace
        assert len(tree.root.children) == 1


@pytest.mark.asyncio
async def test_trace_viewer_search_by_trace_id():
    trace_groups = [
        _make_test_trace_group('trace-123', span_count=2),
        _make_test_trace_group('trace-456', span_count=2),
    ]
    screen = TraceViewerScreen(trace_groups)
    app = LogTailApp()

    async with app.run_test() as pilot:
        app.push_screen(screen)
        await pilot.pause()

        search_input = app.screen.query_one('#trace_search', Input)
        search_input.value = '123'
        await pilot.pause()

        tree = app.screen.query_one('#trace_tree', Tree)
        assert len(tree.root.children) == 1


@pytest.mark.asyncio
async def test_trace_viewer_search_by_service():
    # Create traces with different services
    base_time = datetime.now(UTC)
    trace1_spans = [
        log_event_to_trace_span(
            _make_test_event('trace-1', service_name='api-service', timestamp=base_time),
            'trace-1',
        ),
    ]
    trace2_spans = [
        log_event_to_trace_span(
            _make_test_event('trace-2', service_name='db-service', timestamp=base_time),
            'trace-2',
        ),
    ]

    trace_groups = [
        TraceGroup(
            trace_id='trace-1',
            spans=trace1_spans,
            start_time=base_time,
            end_time=base_time,
            duration_ms=0.0,
            service_names={'api-service'},
            error_count=0,
            span_count=1,
        ),
        TraceGroup(
            trace_id='trace-2',
            spans=trace2_spans,
            start_time=base_time,
            end_time=base_time,
            duration_ms=0.0,
            service_names={'db-service'},
            error_count=0,
            span_count=1,
        ),
    ]

    screen = TraceViewerScreen(trace_groups)
    app = LogTailApp()

    async with app.run_test() as pilot:
        app.push_screen(screen)
        await pilot.pause()

        search_input = app.screen.query_one('#trace_search', Input)
        search_input.value = 'api'
        await pilot.pause()

        tree = app.screen.query_one('#trace_tree', Tree)
        assert len(tree.root.children) == 1


@pytest.mark.asyncio
async def test_trace_viewer_next_error():
    trace_group = _make_test_trace_group('trace-1', span_count=5, error_count=2)
    screen = TraceViewerScreen([trace_group])
    app = LogTailApp()

    async with app.run_test() as pilot:
        app.push_screen(screen)
        await pilot.pause()

        # Press 'n' to jump to next error
        await pilot.press('n')
        await pilot.pause()

        status = app.screen.query_one('#trace_status', Label)
        # Should show error count or navigation message
        assert status is not None


@pytest.mark.asyncio
async def test_trace_viewer_close():
    trace_group = _make_test_trace_group()
    screen = TraceViewerScreen([trace_group])
    app = LogTailApp()

    async with app.run_test() as pilot:
        app.push_screen(screen)
        await pilot.pause()

        # Press Escape to close
        await pilot.press('escape')
        await pilot.pause()

        # Should return to main screen
        assert app.screen != screen


@pytest.mark.asyncio
async def test_trace_viewer_empty_traces():
    screen = TraceViewerScreen([])
    app = LogTailApp()

    async with app.run_test() as pilot:
        app.push_screen(screen)
        await pilot.pause()

        status = app.screen.query_one('#trace_status', Label)
        # Should show no traces message
        assert '0 traces' in str(status.render())


@pytest.mark.asyncio
async def test_trace_viewer_single_trace():
    trace_group = _make_test_trace_group()
    screen = TraceViewerScreen([trace_group])
    app = LogTailApp()

    async with app.run_test() as pilot:
        app.push_screen(screen)
        await pilot.pause()

        tree = app.screen.query_one('#trace_tree', Tree)
        assert len(tree.root.children) == 1


@pytest.mark.asyncio
async def test_trace_viewer_large_trace():
    # Create trace with many spans
    trace_group = _make_test_trace_group('trace-large', span_count=50)
    screen = TraceViewerScreen([trace_group])
    app = LogTailApp()

    async with app.run_test() as pilot:
        app.push_screen(screen)
        await pilot.pause()

        tree = app.screen.query_one('#trace_tree', Tree)
        # Should handle large trace without errors
        assert len(tree.root.children) == 1


@pytest.mark.asyncio
async def test_trace_viewer_chronological_ordering():
    # Create trace with known timestamps
    base_time = datetime.now(UTC)
    events = [
        _make_test_event('trace-1', timestamp=base_time + timedelta(seconds=2)),
        _make_test_event('trace-1', timestamp=base_time),
        _make_test_event('trace-1', timestamp=base_time + timedelta(seconds=1)),
    ]
    spans = [log_event_to_trace_span(e, 'trace-1') for e in events]

    trace_group = TraceGroup(
        trace_id='trace-1',
        spans=sorted(spans, key=lambda s: s.log_event.timestamp),
        start_time=base_time,
        end_time=base_time + timedelta(seconds=2),
        duration_ms=2000.0,
        service_names={'test-service'},
        error_count=0,
        span_count=3,
    )

    screen = TraceViewerScreen([trace_group])
    app = LogTailApp()

    async with app.run_test() as pilot:
        app.push_screen(screen)
        await pilot.pause()

        # Verify spans are in chronological order in TraceGroup
        timestamps = [s.log_event.timestamp for s in trace_group.spans]
        assert timestamps == sorted(timestamps)


@pytest.mark.asyncio
async def test_trace_viewer_status_updates():
    trace_group = _make_test_trace_group()
    screen = TraceViewerScreen([trace_group])
    app = LogTailApp()

    async with app.run_test() as pilot:
        app.push_screen(screen)
        await pilot.pause()

        status = app.screen.query_one('#trace_status', Label)

        # Initial status
        assert '1 traces' in str(status.render())

        # Expand all
        await pilot.press('e')
        await pilot.pause()
        assert 'Expanded all nodes' in str(status.render())


@pytest.mark.asyncio
async def test_trace_viewer_help():
    trace_group = _make_test_trace_group()
    screen = TraceViewerScreen([trace_group])
    app = LogTailApp()

    async with app.run_test() as pilot:
        app.push_screen(screen)
        await pilot.pause()

        # Press '?' to show help
        await pilot.press('question_mark')
        await pilot.pause()

        # Should show notification (can't easily test notification content)
        # Just verify it doesn't crash
        assert app.screen == screen


@pytest.mark.asyncio
async def test_trace_viewer_enter_shows_span_detail():
    """Test that action_show_span_detail pushes RecordDetailScreen for span nodes."""
    trace_group = _make_test_trace_group('trace-1', span_count=3)
    screen = TraceViewerScreen([trace_group])
    app = LogTailApp()

    async with app.run_test() as pilot:
        app.push_screen(screen)
        await pilot.pause()

        tree = app.screen.query_one('#trace_tree', Tree)

        # Expand all to reveal all nodes
        await pilot.press('e')
        await pilot.pause()

        # Navigate down through the tree to find a span node
        # Tree structure: trace -> service -> span (span is leaf)
        # Starting from root (or first trace if show_root=False)
        for _ in range(10):  # Navigate up to 10 nodes to find a span
            await pilot.press('down')
            await pilot.pause()

            cursor_node = tree.cursor_node
            if cursor_node and cursor_node.data and cursor_node.data.get('type') == 'span':
                break

        # Verify we're on a span node
        cursor_node = tree.cursor_node
        assert cursor_node is not None
        assert cursor_node.data is not None
        assert cursor_node.data.get('type') == 'span', f'Expected span node, got {cursor_node.data.get("type")}'

        # Call the action directly instead of pressing enter (more reliable in tests)
        screen.action_show_span_detail()
        await pilot.pause()

        # Check that RecordDetailScreen was pushed
        # The screen should have changed to RecordDetailScreen
        screen_type_name = type(app.screen).__name__
        assert isinstance(app.screen, RecordDetailScreen), f'Expected RecordDetailScreen but got {screen_type_name}'
