"""Unit tests for the record detail modal screen."""

from datetime import UTC, datetime, timedelta

import pytest
from textual.app import App
from textual.pilot import Pilot
from textual.widgets import DataTable, Static

from tail_cw.aws.events import LogEvent
from tail_cw.cli import Session
from tail_cw.config import TailCWConfig
from tail_cw.tui.logs_screen import LogsScreen
from tail_cw.tui.navigation import NavTarget, ViewKind
from tail_cw.tui.record_detail import RecordDetailScreen, field_summary
from tail_cw.tui.shell import TailCWApp
from tail_cw.tui.views import build_screen
from tests.factories import make_event, make_events


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


def _visible_text(app: App[None]) -> str:
    """Everything the modal is currently showing, folded sections excluded."""
    return '\n'.join(str(static.render()) for static in app.screen.query(Static) if static.display)


def test_field_summary_labels_every_field_and_carries_no_message():
    event = make_event(log_group='/aws/lambda/fn', log_stream='2025/01/15/stream', message='body')

    summary = field_summary(event).plain

    assert '/aws/lambda/fn' in summary
    assert '2025/01/15/stream' in summary
    assert 'body' not in summary


def test_field_summary_says_when_cloudwatch_gave_no_ingestion_time():
    assert 'N/A' in field_summary(make_event(ingestion_offset=None)).plain


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'message',
    [
        'Plain text log message',
        '{"level":"INFO", invalid}',
        'Line 1\nLine 2\nLine 3',
        'Special: <>&"\\n\\t☃',
        'A' * 2000,
        '',
    ],
)
async def test_a_message_that_is_not_json_shows_as_itself(message):
    """Nothing else can reproduce the line, so it is shown rather than folded."""
    app = _HostApp()

    async with app.run_test() as pilot:
        app.push_screen(RecordDetailScreen(make_event(message=message)))
        await pilot.pause()

        assert message in _visible_text(app)


@pytest.mark.asyncio
async def test_the_parsed_payload_leads_and_the_raw_line_folds_away():
    """For a structlog record the two are the same information twice."""
    raw = '{"level":"INFO","msg":"test event","index":42}'
    app = _HostApp()

    async with app.run_test() as pilot:
        app.push_screen(RecordDetailScreen(make_event(message=raw)))
        await pilot.pause()
        shown = _visible_text(app)

        assert '"level"' in shown
        assert '"test event"' in shown
        assert raw not in shown, 'the raw copy of the same payload starts folded'

        await pilot.press('r')
        await pilot.pause()

        assert raw in _visible_text(app)


@pytest.mark.asyncio
@pytest.mark.parametrize('key', ['escape', 'q'])
async def test_the_modal_closes_on_a_key(key):
    app = _HostApp()

    async with app.run_test() as pilot:
        depth = len(app.screen_stack)
        app.push_screen(RecordDetailScreen(make_event()))
        await pilot.pause()
        assert len(app.screen_stack) == depth + 1

        await pilot.press(key)
        await pilot.pause()

        assert len(app.screen_stack) == depth


@pytest.mark.asyncio
async def test_copy_puts_both_forms_of_the_message_on_the_clipboard():
    event = make_event(message='{"level":"INFO","msg":"copied"}')
    app = _HostApp()

    async with app.run_test() as pilot:
        app.push_screen(RecordDetailScreen(event))
        await pilot.pause()

        await pilot.press('c')
        await pilot.pause()

        assert event.message in app.clipboard
        assert 'parsed JSON' in app.clipboard


@pytest.mark.asyncio
async def test_modal_from_app_integration():
    """Enter on a selected row opens the detail modal over the log view."""
    events = make_events(['First event', 'Second event'])
    app = _logs_app()

    async with app.run_test() as pilot:
        screen = await _open_logs(app, pilot, events)
        table = screen.query_one('#log_table', DataTable)
        assert table.cursor_row >= 0
        initial_depth = len(app.screen_stack)

        await pilot.press('enter')
        await pilot.pause()

        assert len(app.screen_stack) == initial_depth + 1
        assert isinstance(app.screen, RecordDetailScreen)
        assert 'Second event' in _visible_text(app), 'the modal shows the row the cursor is on'

        await pilot.press('escape')
        await pilot.pause()
        assert len(app.screen_stack) == initial_depth


@pytest.mark.asyncio
async def test_modal_multiple_open_close():
    """The modal can be opened and dismissed repeatedly without leaking screens."""
    events = [make_event(f'Message {index}') for index in range(3)]
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
