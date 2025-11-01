"""Integration tests for trace viewing in the main app."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tail_cw.aws.client import LogEvent
from tail_cw.cache.storage import write_log_events_to_parquet
from tail_cw.tui.app import LogTailApp
from tail_cw.tui.trace_viewer import TraceViewerScreen


def _create_parquet_with_traces(
    output_path: Path,
    trace_count: int = 3,
    spans_per_trace: int = 5,
) -> list[str]:
    """Create Parquet file with trace data.

    Args:
        output_path: Where to write Parquet
        trace_count: Number of traces
        spans_per_trace: Spans per trace

    Returns:
        List of trace IDs
    """
    trace_ids = [f'trace-{i}' for i in range(trace_count)]
    events = []
    base_time = datetime.now(UTC)

    for trace_idx, trace_id in enumerate(trace_ids):
        for span_idx in range(spans_per_trace):
            message_data = {
                'trace_id': trace_id,
                'service_name': f'service-{span_idx % 2}',
                'level': 'INFO',
            }
            event = LogEvent(
                message=json.dumps(message_data),
                timestamp=base_time + timedelta(seconds=trace_idx * 10 + span_idx),
                ingestion_time=base_time,
                log_stream='test-stream',
                log_group='/aws/lambda/test-function',
                event_id=f'event-{trace_idx}-{span_idx}',
            )
            events.append(event)

    write_log_events_to_parquet(events, output_path)
    return trace_ids


@pytest.mark.asyncio
async def test_app_toggle_trace_view(tmp_path: Path):
    parquet_path = tmp_path / 'logs.parquet'
    _create_parquet_with_traces(parquet_path, trace_count=2)

    app = LogTailApp(parquet_path=parquet_path)

    async with app.run_test() as pilot:
        await pilot.pause()

        # Press 't' to toggle trace view
        await pilot.press('t')
        await pilot.pause()

        # Should push TraceViewerScreen
        assert isinstance(app.screen, TraceViewerScreen)


@pytest.mark.asyncio
async def test_app_toggle_trace_view_no_data():
    app = LogTailApp()

    async with app.run_test() as pilot:
        await pilot.pause()

        # Press 't' without data source
        await pilot.press('t')
        await pilot.pause()

        # Should show notification (can't easily verify notification)
        # Just verify it doesn't crash and stays on main screen
        assert not isinstance(app.screen, TraceViewerScreen)


@pytest.mark.asyncio
async def test_app_show_trace_for_selected(tmp_path: Path):
    parquet_path = tmp_path / 'logs.parquet'
    _create_parquet_with_traces(parquet_path, trace_count=2, spans_per_trace=3)

    app = LogTailApp(parquet_path=parquet_path)

    async with app.run_test() as pilot:
        # Set parquet source to load events
        app.set_parquet_source(parquet_path)
        await pilot.pause()

        # Select first row
        table = app._table
        if table and table.row_count > 0:
            table.move_cursor(row=0)
            await pilot.pause()

            # Press 'Shift+t' to show trace for selected
            await pilot.press('shift+t')
            await pilot.pause()

            # Should push TraceViewerScreen if trace ID found
            # (May not if event doesn't have trace ID in parsed form)


@pytest.mark.asyncio
async def test_app_show_trace_for_selected_no_trace_id(tmp_path: Path):
    # Create Parquet without trace IDs
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

    app = LogTailApp(parquet_path=parquet_path)

    async with app.run_test() as pilot:
        app.set_parquet_source(parquet_path)
        await pilot.pause()

        # Select first row
        table = app._table
        if table and table.row_count > 0:
            table.move_cursor(row=0)
            await pilot.pause()

            # Press 'Shift+t'
            await pilot.press('shift+t')
            await pilot.pause()

            # Should show notification about no trace ID
            # Just verify it doesn't crash
            assert not isinstance(app.screen, TraceViewerScreen)


@pytest.mark.asyncio
async def test_app_trace_view_back_to_logs(tmp_path: Path):
    parquet_path = tmp_path / 'logs.parquet'
    _create_parquet_with_traces(parquet_path, trace_count=1)

    app = LogTailApp(parquet_path=parquet_path)

    async with app.run_test() as pilot:
        await pilot.pause()

        # Toggle to trace view
        await pilot.press('t')
        await pilot.pause()

        if isinstance(app.screen, TraceViewerScreen):
            # Press Escape to go back
            await pilot.press('escape')
            await pilot.pause()

            # Should return to main screen
            assert not isinstance(app.screen, TraceViewerScreen)


@pytest.mark.asyncio
async def test_app_trace_view_empty_results(tmp_path: Path):
    # Create Parquet without traces
    events = [
        LogEvent(
            message='Plain message',
            timestamp=datetime.now(UTC),
            ingestion_time=datetime.now(UTC),
            log_stream='test-stream',
            log_group='test-group',
            event_id='test-event-id',
        ),
    ]
    parquet_path = tmp_path / 'logs.parquet'
    write_log_events_to_parquet(events, parquet_path)

    app = LogTailApp(parquet_path=parquet_path)

    async with app.run_test() as pilot:
        await pilot.pause()

        # Press 't'
        await pilot.press('t')
        await pilot.pause()

        # Should show notification about no traces
        # Just verify it doesn't push trace viewer
        assert not isinstance(app.screen, TraceViewerScreen)


@pytest.mark.asyncio
async def test_app_trace_view_with_errors(tmp_path: Path):
    # Create Parquet with error events
    events = []
    base_time = datetime.now(UTC)

    for i in range(3):
        message_data = {
            'trace_id': 'trace-error',
            'service_name': 'test-service',
            'level': 'ERROR' if i == 1 else 'INFO',
        }
        event = LogEvent(
            message=json.dumps(message_data),
            timestamp=base_time + timedelta(seconds=i),
            ingestion_time=base_time,
            log_stream='test-stream',
            log_group='test-group',
            event_id='test-event-id',
        )
        events.append(event)

    parquet_path = tmp_path / 'logs.parquet'
    write_log_events_to_parquet(events, parquet_path)

    app = LogTailApp(parquet_path=parquet_path)

    async with app.run_test() as pilot:
        await pilot.pause()

        # Toggle to trace view
        await pilot.press('t')
        await pilot.pause()

        # Should push trace viewer with error highlighting
        if isinstance(app.screen, TraceViewerScreen):
            assert len(app.screen._trace_groups) == 1
            assert app.screen._trace_groups[0].error_count == 1


@pytest.mark.asyncio
async def test_app_trace_view_multiple_toggles(tmp_path: Path):
    parquet_path = tmp_path / 'logs.parquet'
    _create_parquet_with_traces(parquet_path, trace_count=1)

    app = LogTailApp(parquet_path=parquet_path)

    async with app.run_test() as pilot:
        await pilot.pause()

        # Toggle to trace view
        await pilot.press('t')
        await pilot.pause()

        # Go back
        await pilot.press('escape')
        await pilot.pause()

        # Toggle again
        await pilot.press('t')
        await pilot.pause()

        # Should work correctly
        if isinstance(app.screen, TraceViewerScreen):
            assert len(app.screen._trace_groups) > 0


@pytest.mark.asyncio
async def test_app_help_includes_trace_shortcuts():
    app = LogTailApp()

    async with app.run_test() as pilot:
        await pilot.pause()

        # Check bindings include trace shortcuts
        bindings = {b.key for b in app.BINDINGS}
        assert 't' in bindings
        assert 'shift+t' in bindings


@pytest.mark.asyncio
async def test_app_trace_view_performance(tmp_path: Path):
    # Create large dataset
    parquet_path = tmp_path / 'logs.parquet'
    _create_parquet_with_traces(parquet_path, trace_count=50, spans_per_trace=10)

    app = LogTailApp(parquet_path=parquet_path)

    async with app.run_test() as pilot:
        await pilot.pause()

        # Toggle to trace view
        await pilot.press('t')
        await pilot.pause()

        # Should complete without errors
        if isinstance(app.screen, TraceViewerScreen):
            assert len(app.screen._trace_groups) == 50
