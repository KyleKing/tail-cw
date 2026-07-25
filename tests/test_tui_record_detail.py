"""Unit tests for the record detail modal screen."""

from datetime import UTC, datetime, timedelta

import pytest
from textual.app import App
from textual.pilot import Pilot
from textual.widgets import DataTable, Static

from tail_cw.aws.client import LogEvent
from tail_cw.cli import Session
from tail_cw.config import TailCWConfig
from tail_cw.tui.logs_screen import LogsScreen
from tail_cw.tui.navigation import NavTarget, ViewKind
from tail_cw.tui.record_detail import RecordDetailScreen
from tail_cw.tui.shell import TailCWApp
from tail_cw.tui.views import build_screen

_SENTINEL = object()


class _HostApp(App[None]):
    """A bare host for driving the modal screens under test."""


def _logs_app() -> TailCWApp:
    """Build a shell whose opening view is the log view over one group."""
    now = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
    return TailCWApp(
        TailCWConfig(),
        Session(start=now - timedelta(hours=1), end=now),
        build_screen=build_screen,
        target=NavTarget(kind=ViewKind.LOGS, label='logs /aws/lambda/test', payload=('/aws/lambda/test',)),
    )


async def _open_logs(app: TailCWApp, pilot: Pilot[None], events: list[LogEvent]) -> LogsScreen:
    """Settle the app, load the events, and put the cursor on a row."""
    await pilot.pause()
    screen = app.screen
    assert isinstance(screen, LogsScreen)
    screen.load_events(events)
    await pilot.pause()
    screen.query_one('#log_table', DataTable).focus()
    await pilot.pause()
    await pilot.press('down')
    await pilot.pause()
    return screen


def _make_test_event(
    timestamp: datetime | None = None,
    message: str = 'Test message',
    log_group: str = '/aws/test/group',
    log_stream: str = 'stream-0',
    event_id: str = 'event-0001',
    *,
    ingestion_time: datetime | object | None = _SENTINEL,
) -> LogEvent:
    """Create a test LogEvent with default or custom values.

    Args:
        timestamp: Event timestamp (default: 2025-01-15 10:00:00 UTC)
        message: Log message (default: "Test message")
        log_group: Log group name
        log_stream: Log stream name
        event_id: Event ID
        ingestion_time: Ingestion timestamp (default: 1 second after event timestamp,
            pass None explicitly to set to None)

    Returns:
        LogEvent instance
    """
    if timestamp is None:
        timestamp = datetime(2025, 1, 15, 10, 0, 0, tzinfo=UTC)
    if ingestion_time is _SENTINEL:
        ingestion_time_value: datetime | None = datetime(2025, 1, 15, 10, 0, 1, tzinfo=UTC)
    else:
        ingestion_time_value = ingestion_time  # type: ignore[assignment]

    return LogEvent(
        timestamp=timestamp,
        message=message,
        log_group=log_group,
        log_stream=log_stream,
        event_id=event_id,
        ingestion_time=ingestion_time_value,
    )


def test_modal_initialization():
    """Test modal creation."""
    event = _make_test_event()
    modal = RecordDetailScreen(event)

    assert modal._log_event == event


@pytest.mark.asyncio
async def test_modal_compose_structure():
    """Test modal UI structure."""
    event = _make_test_event()
    app = _HostApp()

    async with app.run_test() as pilot:
        # Push the modal
        app.push_screen(RecordDetailScreen(event))
        await pilot.pause()

        # Query for widgets
        dialog = app.screen.query_one('#dialog')
        content = app.screen.query_one('#content')
        close_button = app.screen.query_one('#close')

        assert dialog is not None
        assert content is not None
        assert close_button is not None


@pytest.mark.asyncio
async def test_modal_displays_event_details():
    """Test content formatting."""
    event = _make_test_event(
        event_id='test-12345',
        log_group='/aws/lambda/my-function',
        log_stream='2025/01/15/stream',
        message='Test log message',
    )
    app = _HostApp()

    async with app.run_test() as pilot:
        app.push_screen(RecordDetailScreen(event))
        await pilot.pause()

        content = app.screen.query_one('#content', Static)
        content_text = str(content.render())

        # Check all fields are present
        assert 'Event ID:' in content_text
        assert 'test-12345' in content_text
        assert 'Timestamp:' in content_text
        assert '2025-01-15' in content_text
        assert 'Log Group:' in content_text
        assert '/aws/lambda/my-function' in content_text
        assert 'Log Stream:' in content_text
        assert '2025/01/15/stream' in content_text
        assert 'Message:' in content_text
        assert 'Test log message' in content_text


@pytest.mark.asyncio
async def test_modal_displays_jsonl_message():
    """Test JSON message parsing."""
    json_message = '{"level":"INFO","msg":"test event","index":42}'
    event = _make_test_event(message=json_message)
    app = _HostApp()

    async with app.run_test() as pilot:
        app.push_screen(RecordDetailScreen(event))
        await pilot.pause()

        content = app.screen.query_one('#content', Static)
        content_text = str(content.render())

        # Should contain both raw and parsed JSON
        assert 'Message (raw):' in content_text
        assert 'Message (parsed JSON):' in content_text
        assert json_message in content_text
        assert '"level"' in content_text
        assert '"INFO"' in content_text


@pytest.mark.asyncio
async def test_modal_displays_plain_text_message():
    """Test plain text message."""
    event = _make_test_event(message='Plain text log message')
    app = _HostApp()

    async with app.run_test() as pilot:
        app.push_screen(RecordDetailScreen(event))
        await pilot.pause()

        content = app.screen.query_one('#content', Static)
        content_text = str(content.render())

        # Should contain the message
        assert 'Plain text log message' in content_text

        # Should NOT have parsed JSON section
        assert 'Message (parsed JSON):' not in content_text


@pytest.mark.asyncio
async def test_modal_close_button():
    """Test close button functionality."""
    event = _make_test_event()
    app = _HostApp()

    async with app.run_test() as pilot:
        # Initial screen stack depth
        initial_depth = len(app.screen_stack)

        # Push modal
        app.push_screen(RecordDetailScreen(event))
        await pilot.pause()

        # Screen stack should be deeper
        assert len(app.screen_stack) == initial_depth + 1

        # Click close button
        await pilot.click('#close')
        await pilot.pause()

        # Screen stack should be back to original depth
        assert len(app.screen_stack) == initial_depth


@pytest.mark.asyncio
async def test_modal_escape_key():
    """Test Escape key dismisses modal."""
    event = _make_test_event()
    app = _HostApp()

    async with app.run_test() as pilot:
        initial_depth = len(app.screen_stack)

        app.push_screen(RecordDetailScreen(event))
        await pilot.pause()

        assert len(app.screen_stack) == initial_depth + 1

        # Press Escape
        await pilot.press('escape')
        await pilot.pause()

        # Modal should be dismissed
        assert len(app.screen_stack) == initial_depth


@pytest.mark.asyncio
async def test_modal_q_key():
    """Test 'q' key dismisses modal."""
    event = _make_test_event()
    app = _HostApp()

    async with app.run_test() as pilot:
        initial_depth = len(app.screen_stack)

        app.push_screen(RecordDetailScreen(event))
        await pilot.pause()

        # Press 'q'
        await pilot.press('q')
        await pilot.pause()

        # Modal should be dismissed
        assert len(app.screen_stack) == initial_depth


@pytest.mark.asyncio
async def test_modal_copy_to_clipboard():
    """Test copy binding places the formatted event details on the clipboard."""
    event = _make_test_event()
    app = _HostApp()

    async with app.run_test() as pilot:
        app.push_screen(RecordDetailScreen(event))
        await pilot.pause()

        # Press 'c' for copy
        await pilot.press('c')
        await pilot.pause()

        assert event.message in app.clipboard
        assert event.event_id in app.clipboard


@pytest.mark.asyncio
async def test_modal_with_none_ingestion_time():
    """Test event with None ingestion_time."""
    event = _make_test_event(ingestion_time=None)
    app = _HostApp()

    async with app.run_test() as pilot:
        app.push_screen(RecordDetailScreen(event))
        await pilot.pause()

        content = app.screen.query_one('#content', Static)
        content_text = str(content.render())

        # Should show 'N/A' for ingestion time
        assert 'Ingestion Time: N/A' in content_text


@pytest.mark.asyncio
async def test_modal_with_long_message():
    """Test with very long message."""
    long_message = 'A' * 2000
    event = _make_test_event(message=long_message)
    app = _HostApp()

    async with app.run_test() as pilot:
        app.push_screen(RecordDetailScreen(event))
        await pilot.pause()

        content = app.screen.query_one('#content', Static)
        content_text = str(content.render())

        # Full message should be displayed (not truncated)
        assert long_message in content_text


@pytest.mark.asyncio
async def test_modal_with_special_characters():
    """Test message with special characters."""
    special_message = 'Special: <>&"\\n\\t\u2603'
    event = _make_test_event(message=special_message)
    app = _HostApp()

    async with app.run_test() as pilot:
        app.push_screen(RecordDetailScreen(event))
        await pilot.pause()

        content = app.screen.query_one('#content', Static)
        content_text = str(content.render())

        # All characters should be displayed
        assert '\u2603' in content_text or '☃' in content_text


@pytest.mark.asyncio
async def test_modal_from_app_integration():
    """Enter on a selected row opens the detail modal over the log view."""
    events = [
        _make_test_event(event_id='event-0001', message='First event'),
        _make_test_event(event_id='event-0002', message='Second event'),
    ]
    app = _logs_app()

    async with app.run_test() as pilot:
        screen = await _open_logs(app, pilot, events)
        table = screen.query_one('#log_table', DataTable)
        assert table.cursor_row >= 0
        initial_depth = len(app.screen_stack)

        screen.action_show_detail()
        await pilot.pause()

        assert len(app.screen_stack) == initial_depth + 1
        assert isinstance(app.screen, RecordDetailScreen)
        content_text = str(app.screen.query_one('#content', Static).render())
        assert 'event-000' in content_text

        await pilot.press('escape')
        await pilot.pause()
        assert len(app.screen_stack) == initial_depth


@pytest.mark.asyncio
async def test_modal_multiple_open_close():
    """The modal can be opened and dismissed repeatedly without leaking screens."""
    events = [_make_test_event(event_id=f'event-{index:04d}') for index in range(3)]
    app = _logs_app()

    async with app.run_test() as pilot:
        screen = await _open_logs(app, pilot, events)
        initial_depth = len(app.screen_stack)

        for _ in range(3):
            screen.action_show_detail()
            await pilot.pause()
            assert len(app.screen_stack) == initial_depth + 1
            await pilot.press('escape')
            await pilot.pause()
            assert len(app.screen_stack) == initial_depth


@pytest.mark.asyncio
async def test_modal_with_empty_message():
    """Test event with empty message."""
    event = _make_test_event(message='')
    app = _HostApp()

    async with app.run_test() as pilot:
        app.push_screen(RecordDetailScreen(event))
        await pilot.pause()

        content = app.screen.query_one('#content', Static)
        content_text = str(content.render())

        # Should have Message label, even if empty
        assert 'Message:' in content_text


@pytest.mark.asyncio
async def test_modal_with_multiline_message():
    """Test message with multiple lines."""
    multiline_message = """Line 1
Line 2
Line 3"""
    event = _make_test_event(message=multiline_message)
    app = _HostApp()

    async with app.run_test() as pilot:
        app.push_screen(RecordDetailScreen(event))
        await pilot.pause()

        content = app.screen.query_one('#content', Static)
        content_text = str(content.render())

        # All lines should be present
        assert 'Line 1' in content_text
        assert 'Line 2' in content_text
        assert 'Line 3' in content_text


@pytest.mark.asyncio
async def test_modal_with_malformed_json():
    """Test message with malformed JSON."""
    malformed_json = '{"level":"INFO", invalid}'
    event = _make_test_event(message=malformed_json)
    app = _HostApp()

    async with app.run_test() as pilot:
        app.push_screen(RecordDetailScreen(event))
        await pilot.pause()

        content = app.screen.query_one('#content', Static)
        content_text = str(content.render())

        # Should display as plain text (not crash on parse error)
        assert malformed_json in content_text
        # Should NOT have parsed JSON section
        assert 'Message (parsed JSON):' not in content_text
