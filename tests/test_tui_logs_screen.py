# ruff: file-ignore[unused-async] - the service fakes conform to awaitable signatures
"""Tests for the log view screen, historical and live."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from textual.pilot import Pilot
from textual.widgets import DataTable, Label

from tail_cw.aws.events import LogEvent
from tail_cw.cache.storage import write_log_events_to_parquet
from tail_cw.cli import Session
from tail_cw.config import TailCWConfig, TUIConfig
from tail_cw.query.expression import parse_query
from tail_cw.query.facets import FieldFacet, count_by_field, discover_field_paths, worth_showing
from tail_cw.tui.facets_panel import FacetsPanel
from tail_cw.tui.logs_screen import LogsScreen, ProgressUpdate, _field_syntax_hint
from tail_cw.tui.navigation import NavTarget, ViewKind
from tail_cw.tui.shell import ShellServices, TailCWApp
from tail_cw.tui.trace_viewer import TraceViewerScreen
from tail_cw.tui.views import build_screen
from tests.tui_support import running

from .asyncsupport import streams

BASE_TIME = datetime(2025, 1, 15, 10, 0, 0, tzinfo=UTC)
LIVE_BASE_TIME = datetime(2026, 7, 5, 12, 0, 0, tzinfo=UTC)
DEFAULT_GROUP = '/aws/test/group0'


def _make_test_log_events(count: int = 5) -> list[LogEvent]:
    events = []
    for index in range(count):
        message = (
            f'Test log message {index}'
            if index % 2 == 0
            else f'{{"level":"INFO","index":{index},"message":"Test JSON {index}"}}'
        )
        timestamp = BASE_TIME + timedelta(seconds=index)
        events.append(
            LogEvent(
                timestamp=timestamp,
                message=message,
                log_group=f'/aws/test/group{index % 2}',
                log_stream=f'stream-{index}',
                ingestion_time=timestamp + timedelta(milliseconds=1),
            ),
        )
    return events


def _json_event(event: str, *, level: str = 'INFO', offset: int = 0) -> LogEvent:
    timestamp = BASE_TIME + timedelta(seconds=offset)
    return LogEvent(
        timestamp=timestamp,
        message=f'{{"level":"{level}","event":"{event}"}}',
        log_group=DEFAULT_GROUP,
        log_stream='stream-0',
        ingestion_time=None,
    )


def _make_live_events(count: int, *, offset: int = 0) -> list[LogEvent]:
    return [
        LogEvent(
            log_group='/aws/test/group',
            log_stream='stream-live',
            timestamp=LIVE_BASE_TIME + timedelta(seconds=offset + index),
            message=f'live message {offset + index}',
            ingestion_time=None,
        )
        for index in range(count)
    ]


def _session(**overrides: Any) -> Session:
    defaults: dict[str, Any] = {
        'start': BASE_TIME,
        'end': BASE_TIME + timedelta(hours=1),
        'selected_groups': [DEFAULT_GROUP],
    }
    defaults.update(overrides)
    return Session(**defaults)


def _resolving_to(paths: Sequence[Path]) -> ShellServices:
    async def resolve(groups: Sequence[str], start: datetime, end: datetime) -> list[Path]:
        del groups, start, end
        return list(paths)

    return ShellServices(resolve_logs=resolve)


def _make_app(
    log_groups: Sequence[str] = (DEFAULT_GROUP,),
    *,
    live: bool = False,
    config: TailCWConfig | None = None,
    services: ShellServices | None = None,
    session: Session | None = None,
    trace_id: str = '',
) -> TailCWApp:
    """Build a shell whose opening view is the log screen over ``log_groups``."""
    label = f'{"tail" if live else "logs"} {log_groups[0] if len(log_groups) == 1 else f"{len(log_groups)} groups"}'
    target = NavTarget(kind=ViewKind.LOGS, label=label, payload=tuple(log_groups), argument=trace_id)
    return TailCWApp(
        config if config is not None else TailCWConfig(),
        session if session is not None else _session(selected_groups=list(log_groups)),
        build_screen=build_screen,
        services=services if services is not None else ShellServices(),
        target=target,
    )


def _search_shown(screen: LogsScreen) -> bool:
    assert screen._search_input is not None
    return bool(screen._search_input.display)


def _cell(app: TailCWApp, key: str) -> object:
    table = app.screen.query_one('#log_table', DataTable)
    return table.get_cell(next(iter(table.rows.keys())), key)


def _logs_screen(app: TailCWApp) -> LogsScreen:
    screen = app.screen
    assert isinstance(screen, LogsScreen)
    return screen


def _write_parquet(events: list[LogEvent], path: Path) -> Path:
    write_log_events_to_parquet(events, path)
    return path


def _small_chunk_config(**overrides: Any) -> TailCWConfig:
    defaults: dict[str, Any] = {
        'chunk_threshold': 4,
        'chunk_size': 2,
        'initial_load_limit': 5,
        'search_limit': 10,
        'trace_limit': 5,
    }
    defaults.update(overrides)
    return TailCWConfig(tui=TUIConfig(**defaults))


def _create_parquet_with_traces(
    output_path: Path,
    trace_count: int = 3,
    spans_per_trace: int = 5,
) -> list[str]:
    trace_ids = [f'trace-{index}' for index in range(trace_count)]
    events = []
    base_time = datetime.now(UTC)

    for trace_idx, trace_id in enumerate(trace_ids):
        for span_idx in range(spans_per_trace):
            message_data = {
                'trace_id': trace_id,
                'service_name': f'service-{span_idx % 2}',
                'level': 'INFO',
            }
            events.append(
                LogEvent(
                    message=json.dumps(message_data),
                    timestamp=base_time + timedelta(seconds=trace_idx * 10 + span_idx),
                    ingestion_time=base_time,
                    log_stream='test-stream',
                    log_group='/aws/lambda/test-function',
                ),
            )

    write_log_events_to_parquet(events, output_path)
    return trace_ids


def test_screen_initialization():
    screen = LogsScreen([DEFAULT_GROUP])

    assert screen.log_groups == [DEFAULT_GROUP]
    assert screen.live_mode is False
    assert screen._log_events == []
    assert screen._table is None


def test_screen_initialization_live():
    screen = LogsScreen(['a', 'b'], live=True)

    assert screen.log_groups == ['a', 'b']
    assert screen.live_mode is True


@pytest.mark.asyncio
async def test_screen_reads_config_from_shell():
    config = _small_chunk_config(chunk_threshold=10, chunk_size=3, initial_load_limit=42, search_limit=77)
    config.trace.trace_id_fields = ['traceId', 'context.trace_id']
    app = _make_app(config=config)

    async with running(app) as _:
        screen = _logs_screen(app)

        assert screen._config is config
        assert screen._config.tui.chunk_threshold == 10
        assert screen._trace_id_fields == ['traceId', 'context.trace_id']


@pytest.mark.asyncio
async def test_screen_uses_default_config():
    app = _make_app()

    async with running(app) as _:
        screen = _logs_screen(app)

        assert isinstance(screen._config, TailCWConfig)
        assert screen._config.tui.chunk_threshold == 5000
        assert screen._trace_id_fields


@pytest.mark.asyncio
async def test_compose_structure():
    app = _make_app()

    async with running(app) as _:
        assert app.screen.query_one('Footer') is not None
        assert app.screen.query_one('#log_table') is not None
        assert app.screen.query_one('#status') is not None
        assert app.screen.query_one('#breadcrumb') is not None


@pytest.mark.asyncio
async def test_progress_update_message():
    app = _make_app()

    async with running(app) as pilot:
        screen = _logs_screen(app)
        screen.post_message(ProgressUpdate(current=25, total=100, status='Loading'))
        await pilot.pause()

        rendered = str(app.screen.query_one('#status', Label).render())
        assert 'Loading' in rendered
        assert '25/100' in rendered


@pytest.mark.asyncio
async def test_table_columns_are_budgeted_for_the_terminal_width():
    """At the 80-column test size only the columns that carry information survive."""
    app = _make_app()

    async with running(app) as _:
        table = app.screen.query_one('#log_table', DataTable)

        assert [col.key for col in table.columns.values()] == ['timestamp', 'severity', 'message']


@pytest.mark.asyncio
async def test_resolved_events_load_on_mount(tmp_path: Path):
    events = _make_test_log_events(5)
    path = _write_parquet(events, tmp_path / 'events.parquet')
    app = _make_app(services=_resolving_to([path]))

    async with running(app) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()

        screen = _logs_screen(app)
        table = app.screen.query_one('#log_table', DataTable)

        assert table.row_count == 5
        assert screen._parquet_paths == [path]
        assert '5 events' in str(app.screen.query_one('#status', Label).render()).lower()


@pytest.mark.asyncio
async def test_multiple_groups_merge_by_timestamp(tmp_path: Path):
    first = [
        LogEvent(
            log_group='/aws/test/a',
            log_stream='stream-a',
            timestamp=BASE_TIME + timedelta(seconds=index * 2),
            message=f'a-{index}',
            ingestion_time=None,
        )
        for index in range(3)
    ]
    second = [
        LogEvent(
            log_group='/aws/test/b',
            log_stream='stream-b',
            timestamp=BASE_TIME + timedelta(seconds=index * 2 + 1),
            message=f'b-{index}',
            ingestion_time=None,
        )
        for index in range(3)
    ]
    paths = [
        _write_parquet(first, tmp_path / 'a.parquet'),
        _write_parquet(second, tmp_path / 'b.parquet'),
    ]
    app = _make_app(['/aws/test/a', '/aws/test/b'], services=_resolving_to(paths))

    async with running(app) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()

        screen = _logs_screen(app)

        assert screen._parquet_paths == paths
        assert [event.message for event in screen._log_events] == ['a-0', 'b-0', 'a-1', 'b-1', 'a-2', 'b-2']
        assert app.screen.query_one('#log_table', DataTable).row_count == 6


@pytest.mark.asyncio
async def test_missing_parquet_paths_report_no_events():
    app = _make_app(services=_resolving_to([]))

    async with running(app) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert app.screen.query_one('#log_table', DataTable).row_count == 0
        assert 'No events found' in str(app.screen.query_one('#status', Label).render())


@pytest.mark.asyncio
async def test_a_slow_load_says_how_long_it_has_waited_and_how_to_stop():
    """A status line reading only "Loading" for forty seconds is indistinguishable from a hang."""
    release = asyncio.Event()

    async def slow_resolve(groups: Sequence[str], start: datetime, end: datetime) -> list[Path]:
        del groups, start, end
        await release.wait()
        return []

    app = _make_app(services=ShellServices(resolve_logs=slow_resolve))

    async with running(app) as pilot:
        await pilot.pause()
        status = app.screen.query_one('#status', Label)
        assert 'esc to stop' in str(status.render())

        await pilot.press('escape')
        await pilot.pause()

        assert [worker for worker in app.workers if worker.group == 'resolve_logs' and worker.is_running] == []
        assert 'Load stopped' in str(status.render())
        release.set()


@pytest.mark.asyncio
async def test_escape_still_goes_back_when_nothing_is_loading(tmp_path: Path):
    """Cancelling must not cost the view its Back key once the load has finished."""
    path = _write_parquet(_make_test_log_events(5), tmp_path / 'events.parquet')
    app = _make_app(services=_resolving_to([path]))

    async with running(app) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        depth = len(app.screen_stack)

        await pilot.press('escape')
        await pilot.pause()

        assert len(app.screen_stack) == depth, 'the log view is the root here, so Back has nowhere to go'
        assert 'Load stopped' not in str(app.screen.query_one('#status', Label).render())


@pytest.mark.parametrize(
    ('query', 'expected'),
    [
        ('level=info', ' · try level:info to match the record field'),
        ('level:info', ''),
        ('a=b c=d', ''),
        ('plain text', ''),
    ],
)
def test_a_fruitless_search_that_reads_like_a_field_suggests_the_field_syntax(query, expected):
    """The table renders a record as key=value, so that is what gets typed into the search box."""
    assert _field_syntax_hint(query) == expected


@pytest.mark.asyncio
async def test_an_error_naming_a_file_does_not_take_the_app_down(tmp_path: Path):
    """Every Polars failure names its file as [/path.parquet], which Rich reads as a closing tag."""

    async def failing_resolve(groups: Sequence[str], start: datetime, end: datetime) -> list[Path]:
        del groups, start, end
        msg = "closing tag '[/tmp/cache_v2_abc.parquet]' does not match any open tag"
        raise RuntimeError(msg)

    app = _make_app(services=ShellServices(resolve_logs=failing_resolve))

    async with running(app) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert 'cache_v2_abc.parquet' in str(app.screen.query_one('#status', Label).render())


@pytest.mark.asyncio
async def test_resolve_failure_reports_error(tmp_path: Path):
    del tmp_path

    async def failing_resolve(groups: Sequence[str], start: datetime, end: datetime) -> list[Path]:
        del groups, start, end
        msg = 'throttled'
        raise RuntimeError(msg)

    app = _make_app(services=ShellServices(resolve_logs=failing_resolve))

    async with running(app) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert 'throttled' in str(app.screen.query_one('#status', Label).render())


@pytest.mark.asyncio
async def test_incremental_loading_with_progress(monkeypatch):
    events = _make_test_log_events(6)
    app = _make_app(config=_small_chunk_config())
    messages: list[str] = []

    async with running(app) as pilot:
        screen = _logs_screen(app)
        original_update = screen._update_status

        def record_status(message: str) -> None:
            messages.append(message)
            original_update(message)

        monkeypatch.setattr(screen, '_update_status', record_status)
        screen.load_events(events)
        table = app.screen.query_one('#log_table', DataTable)
        for _ in range(20):
            await pilot.pause()
            if table.row_count == len(events):
                break

        await app.workers.wait_for_complete()

        assert table.row_count == len(events)

    assert any('Loading events' in message for message in messages)
    assert any(f'Loaded {len(events)} events' in message for message in messages)


@pytest.mark.asyncio
async def test_no_service_shows_no_source_message():
    app = _make_app()

    async with running(app) as _:
        assert app.screen.query_one('#log_table', DataTable).row_count == 0
        assert 'no log source' in str(app.screen.query_one('#status', Label).render()).lower()


@pytest.mark.asyncio
async def test_config_chunk_threshold_drives_worker(monkeypatch):
    app = _make_app(config=_small_chunk_config(chunk_threshold=3, chunk_size=1))
    events = _make_test_log_events(5)

    async with running(app) as pilot:
        screen = _logs_screen(app)
        original_run_worker = screen.run_worker
        flags = {'called': False}

        def tracking_run_worker(awaitable, *args, **kwargs):
            flags['called'] = True
            return original_run_worker(awaitable, *args, **kwargs)

        monkeypatch.setattr(screen, 'run_worker', tracking_run_worker)
        screen.load_events(events)
        await pilot.pause()

    assert flags['called'] is True


@pytest.mark.asyncio
async def test_search_respects_config_limit(monkeypatch, tmp_path: Path):
    parquet_path = tmp_path / 'logs.parquet'
    parquet_path.write_text('', encoding='utf-8')
    events = _make_test_log_events(3)
    captured: dict[str, Any] = {}

    def fake_query(paths, filter_node, limit):
        captured['paths'] = list(paths)
        captured['limit'] = limit
        yield from events

    monkeypatch.setattr('tail_cw.tui.logs_screen.query_parquet_files_to_log_events', fake_query)
    app = _make_app(config=_small_chunk_config(chunk_threshold=5000, chunk_size=1000, search_limit=15))

    async with running(app) as pilot:
        screen = _logs_screen(app)
        screen.load_events([], parquet_paths=[parquet_path])
        await screen._execute_search_query('message')
        await pilot.pause()

        assert captured['limit'] == 15
        assert captured['paths'] == [parquet_path]
        assert len(screen._log_events) == 3


@pytest.mark.asyncio
async def test_search_in_memory_without_parquet():
    app = _make_app()

    async with running(app) as pilot:
        screen = _logs_screen(app)
        screen.load_events(_make_test_log_events(4))
        await screen._execute_search_query('Test log message 2')
        await pilot.pause()

        assert [event.message for event in screen._log_events] == ['Test log message 2']
        assert 'Found 1 matching events' in str(app.screen.query_one('#status', Label).render())


@pytest.mark.asyncio
async def test_empty_search_restores_all_events():
    app = _make_app()

    async with running(app) as pilot:
        screen = _logs_screen(app)
        screen.load_events(_make_test_log_events(4))
        assert screen._search_input is not None
        screen._search_input.value = 'x'
        await pilot.pause()
        screen._search_input.value = ''
        await pilot.pause()

        assert len(screen._log_events) == 4
        assert 'Showing all 4 events' in str(app.screen.query_one('#status', Label).render())


@pytest.mark.asyncio
async def test_search_submit_moves_focus_to_table():
    app = _make_app()

    async with running(app) as pilot:
        screen = _logs_screen(app)
        screen.load_events(_make_test_log_events(3))
        await pilot.press('/')
        await pilot.pause()
        assert screen._search_input is not None
        assert screen._search_input.has_focus, 'the footer advertises / as the way in'
        await pilot.press('enter')
        await pilot.pause()

        table = app.screen.query_one('#log_table', DataTable)
        assert table.has_focus
        assert table.cursor_row == 0


@pytest.mark.asyncio
async def test_keyboard_navigation():
    app = _make_app()

    async with running(app) as pilot:
        screen = _logs_screen(app)
        screen.load_events(_make_test_log_events(5))
        table = app.screen.query_one('#log_table', DataTable)
        table.focus()
        await pilot.pause()

        assert table.cursor_row == 0

        await pilot.press('down')
        assert table.cursor_row == 1

        await pilot.press('down')
        assert table.cursor_row == 2


@pytest.mark.asyncio
async def test_quit_binding():
    app = _make_app()

    async with running(app) as pilot:
        await pilot.press('q')


@pytest.mark.asyncio
async def test_show_detail_with_no_events_pushes_no_modal():
    app = _make_app()

    async with running(app) as pilot:
        await pilot.press('enter')
        await pilot.pause()

        assert len(app.screen_stack) == 2


@pytest.mark.asyncio
async def test_show_detail_opens_modal():
    app = _make_app()

    async with running(app) as pilot:
        screen = _logs_screen(app)
        screen.load_events(_make_test_log_events(3))
        await pilot.pause()
        screen.action_show_detail()
        await pilot.pause()

        assert len(app.screen_stack) == 3


@pytest.mark.asyncio
async def test_focus_search_without_data_notifies():
    app = _make_app()

    async with running(app) as pilot:
        screen = _logs_screen(app)
        await pilot.press('/')
        await pilot.pause()

        assert screen._search_input is not None
        assert not screen._search_input.has_focus


@pytest.mark.asyncio
async def test_focus_search_with_data_focuses_input():
    app = _make_app()

    async with running(app) as pilot:
        screen = _logs_screen(app)
        screen.load_events(_make_test_log_events(2))
        await pilot.pause()
        await pilot.press('/')
        await pilot.pause()

        assert screen._search_input is not None
        assert screen._search_input.has_focus


@pytest.mark.asyncio
async def test_load_events_updates_table_and_status():
    app = _make_app()

    async with running(app) as pilot:
        screen = _logs_screen(app)
        screen.load_events(_make_test_log_events(3))
        await pilot.pause()

        table = app.screen.query_one('#log_table', DataTable)
        status = app.screen.query_one('#status', Label)
        assert table.row_count == 3
        assert '3 events' in str(status.render()).lower()

        screen.load_events(_make_test_log_events(7))
        await pilot.pause()

        assert table.row_count == 7
        assert '7 events' in str(status.render()).lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('keys', 'expected_row'),
    [
        (('j', 'j'), 2),
        (('j', 'j', 'k'), 1),
        (('G',), 4),
        (('G', 'g'), 0),
        (('ctrl+d',), 4),
        (('G', 'ctrl+u'), 0),
    ],
)
async def test_vim_motions_drive_the_row_cursor(keys, expected_row):
    app = _make_app()

    async with running(app) as pilot:
        _logs_screen(app).load_events(_make_test_log_events(5))
        await pilot.pause()
        for key in keys:
            await pilot.press(key)
        await pilot.pause()

        assert app.screen.query_one('#log_table', DataTable).cursor_row == expected_row


@pytest.mark.asyncio
async def test_a_narrower_terminal_re_budgets_the_columns():
    app = _make_app()

    async with running(app) as pilot:
        _logs_screen(app).load_events(_make_test_log_events(2))
        await pilot.pause()
        assert [col.key for col in app.screen.query_one('#log_table', DataTable).columns.values()] == [
            'timestamp',
            'severity',
            'message',
        ]

        await pilot.resize_terminal(200, 24)
        await pilot.pause()

        table = app.screen.query_one('#log_table', DataTable)
        assert [col.key for col in table.columns.values()] == ['timestamp', 'severity', 'log_stream', 'message']
        assert table.row_count == 2, 'the rows survive the re-budget'


@pytest.mark.asyncio
async def test_the_status_line_says_when_the_view_is_capped(tmp_path: Path):
    """`Loaded 1000 events` hid whether that was everything."""
    config = TailCWConfig()
    config.tui.initial_load_limit = 3
    path = _write_parquet(_make_test_log_events(5), tmp_path / 'events.parquet')
    app = _make_app(config=config, services=_resolving_to([path]))

    async with running(app) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert 'capped' in str(app.screen.query_one('#status', Label).render())


@pytest.mark.asyncio
async def test_timestamp_drops_the_date_the_breadcrumb_already_states():
    app = _make_app()

    async with running(app) as pilot:
        screen = _logs_screen(app)
        screen.load_events(_make_test_log_events(1))
        await pilot.pause()

        rendered = str(_cell(app, 'timestamp'))

        assert '10:00:00' in rendered
        assert '2025-01-15' not in rendered, 'the date costs 11 of 80 columns and never varies'


@pytest.mark.asyncio
async def test_message_truncation():
    event = LogEvent(
        timestamp=BASE_TIME,
        message='A' * 200,
        log_group='/aws/test/group',
        log_stream='stream-0',
        ingestion_time=BASE_TIME + timedelta(seconds=1),
    )
    app = _make_app()

    async with running(app) as pilot:
        screen = _logs_screen(app)
        screen.load_events([event])
        await pilot.pause()

        message = str(_cell(app, 'message'))

        assert message.endswith('\u2026'), 'a clipped message has to say it was clipped'
        assert len(message) < len(event.message)


@pytest.mark.asyncio
async def test_batch_loading_many_events():
    app = _make_app()

    async with running(app) as pilot:
        screen = _logs_screen(app)
        screen.load_events(_make_test_log_events(1000))
        await pilot.pause()

        assert app.screen.query_one('#log_table', DataTable).row_count == 1000


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'message',
    ['Test message', 'Special chars: <>&"\\n\\t☃', ''],
)
async def test_awkward_messages_load(message: str):
    event = LogEvent(
        timestamp=BASE_TIME,
        message=message,
        log_group='/aws/test/group',
        log_stream='stream-0',
        ingestion_time=None,
    )
    app = _make_app()

    async with running(app) as pilot:
        screen = _logs_screen(app)
        screen.load_events([event])
        await pilot.pause()

        assert app.screen.query_one('#log_table', DataTable).row_count == 1


@pytest.mark.asyncio
async def test_refresh_extends_window_and_reloads(tmp_path: Path):
    events = _make_test_log_events(4)
    path = _write_parquet(events, tmp_path / 'events.parquet')
    calls: list[tuple[datetime, datetime]] = []

    async def resolve(groups: Sequence[str], start: datetime, end: datetime) -> list[Path]:
        del groups
        calls.append((start, end))
        return [path]

    session = _session()
    app = _make_app(services=ShellServices(resolve_logs=resolve), session=session)

    async with running(app) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press('r')
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert app.screen.query_one('#log_table', DataTable).row_count == 4

    assert len(calls) == 2
    assert calls[1][0] == calls[0][0]
    assert calls[1][1] > calls[0][1]


@pytest.mark.asyncio
async def test_refresh_without_service_reports_no_source():
    app = _make_app()

    async with running(app) as pilot:
        await pilot.press('r')
        await pilot.pause()

        assert 'no log source' in str(app.screen.query_one('#status', Label).render()).lower()


@pytest.mark.asyncio
async def test_the_session_filter_narrows_the_view_on_read(tmp_path: Path):
    """The cached window holds every event, so the filter is applied locally."""
    events = [_json_event('one', level='INFO'), _json_event('two', level='ERROR')]
    path = _write_parquet(events, tmp_path / 'events.parquet')
    app = _make_app(services=_resolving_to([path]))

    async with running(app) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.screen.query_one('#log_table', DataTable).row_count == 2

        app.session.filter_pattern = 'ERROR'
        _logs_screen(app).refresh_view()
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert app.screen.query_one('#log_table', DataTable).row_count == 1


@pytest.mark.asyncio
async def test_view_commands_toggle_live_and_trace():
    app = _make_app(services=ShellServices(live_stream=streams(())))

    async with running(app) as pilot:
        screen = _logs_screen(app)

        assert set(screen.commands()) == {'fields', 'live', 'pivot', 'trace'}
        assert screen.run_view_command('nope', '') is False
        assert screen.run_view_command('live', '') is True
        await pilot.pause()

        assert screen.live_mode is True


@pytest.mark.asyncio
async def test_nav_siblings_cycle_selected_groups():
    app = _make_app(['a'], session=_session(selected_groups=['a', 'b', 'c']))

    async with running(app) as _:
        targets = _logs_screen(app).nav_siblings()

        assert [target.label for target in targets] == ['logs a', 'logs b', 'logs c']
        assert targets[1].payload == ('b',)


@pytest.mark.asyncio
async def test_nav_siblings_lead_with_a_merged_view():
    app = _make_app(['a', 'b'])

    async with running(app) as _:
        targets = _logs_screen(app).nav_siblings()

        assert targets[0].label == 'logs 2 groups'
        assert [target.label for target in targets[1:]] == ['logs a', 'logs b']


@pytest.mark.asyncio
async def test_nav_siblings_use_tail_labels_when_live():
    app = _make_app(['a'], live=True, session=_session(selected_groups=['a', 'b']))

    async with running(app) as _:
        targets = _logs_screen(app).nav_siblings()

        assert [target.label for target in targets] == ['tail a', 'tail b']


@pytest.mark.asyncio
async def test_live_worker_streams_events_into_table():
    events = _make_live_events(5)
    app = _make_app(
        live=True,
        config=TailCWConfig(tui=TUIConfig(live_buffer_limit=100)),
        services=ShellServices(live_stream=streams(events)),
    )

    async with running(app) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()

        screen = _logs_screen(app)
        table = app.screen.query_one('#log_table', DataTable)
        assert table.row_count == 5
        assert screen._live_event_count == 5
        assert list(screen._live_buffer) == events
        assert screen._all_events == events
        assert screen._live_active is False
        status = str(app.screen.query_one('#status', Label).render())
        assert 'Stopped' in status
        assert '5 events' in status


@pytest.mark.asyncio
async def test_live_batched_flush_and_pause_resume():
    app = _make_app(config=TailCWConfig(tui=TUIConfig(live_buffer_limit=100)))

    async with running(app) as pilot:
        screen = _logs_screen(app)
        screen._live_stream_factory = streams(())
        screen._live_active = True

        first_batch = _make_live_events(3)
        screen._pending_live_events.extend(first_batch)
        screen._flush_live_events()

        table = app.screen.query_one('#log_table', DataTable)
        status = app.screen.query_one('#status', Label)
        assert table.row_count == 3
        assert 'Live' in str(status.render())

        await pilot.press('space')
        assert screen._live_paused
        assert 'Paused' in str(status.render())

        second_batch = _make_live_events(2, offset=3)
        screen._pending_live_events.extend(second_batch)
        screen._flush_live_events()

        assert table.row_count == 3
        assert len(screen._live_buffer) == 5
        assert screen._live_event_count == 5

        await pilot.press('space')
        assert 'Live' in str(status.render())
        assert table.row_count == 5
        assert screen._log_events == first_batch + second_batch


@pytest.mark.asyncio
async def test_live_buffer_evicts_oldest_and_rebuilds_table():
    app = _make_app(config=TailCWConfig(tui=TUIConfig(live_buffer_limit=10)))

    async with running(app) as pilot:
        screen = _logs_screen(app)
        screen._live_stream_factory = streams(())
        screen._live_active = True

        screen._pending_live_events.extend(_make_live_events(120))
        screen._flush_live_events()
        await pilot.pause()

        table = app.screen.query_one('#log_table', DataTable)
        assert len(screen._live_buffer) == 10
        assert table.row_count == 10
        assert screen._log_events == _make_live_events(10, offset=110)
        assert screen._all_events[-1].message == 'live message 119'


@pytest.mark.asyncio
async def test_live_search_filters_buffered_events():
    app = _make_app(config=TailCWConfig(tui=TUIConfig(live_buffer_limit=100)))

    async with running(app) as pilot:
        screen = _logs_screen(app)
        screen._live_stream_factory = streams(())
        screen._live_active = True

        screen._pending_live_events.extend(_make_live_events(4))
        screen._flush_live_events()

        results = screen._filter_events_in_memory('live message 2')
        assert [event.message for event in results] == ['live message 2']

        assert screen._search_input is not None
        screen._search_input.value = 'live message'
        screen._pending_live_events.extend(_make_live_events(2, offset=4))
        screen._flush_live_events()

        table = app.screen.query_one('#log_table', DataTable)
        assert table.row_count == 4
        assert len(screen._live_buffer) == 6
        await pilot.pause()


@pytest.mark.asyncio
async def test_live_worker_error_keeps_buffer_browsable():
    async def failing_stream(groups: Sequence[str], pattern: str | None) -> AsyncIterator[LogEvent]:
        del groups, pattern
        for event in _make_live_events(2):
            yield event
        msg = 'session exhausted'
        raise RuntimeError(msg)

    app = _make_app(live=True, services=ShellServices(live_stream=failing_stream))

    async with running(app) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()

        screen = _logs_screen(app)
        assert app.screen.query_one('#log_table', DataTable).row_count == 2
        assert screen._live_active is False
        assert len(screen._live_buffer) == 2
        assert 'Stopped' in str(app.screen.query_one('#status', Label).render())


@pytest.mark.asyncio
async def test_note_live_sampled_sets_flag():
    app = _make_app()

    async with running(app) as _:
        screen = _logs_screen(app)
        screen.note_live_sampled(sampled=True)

        assert screen._live_sampled is True


@pytest.mark.asyncio
async def test_live_toggle_round_trip_preserves_filter(tmp_path: Path):
    path = _write_parquet(_make_test_log_events(3), tmp_path / 'events.parquet')
    resolve_calls: list[tuple[str, ...]] = []
    live_calls: list[tuple[tuple[str, ...], str | None]] = []

    async def resolve(groups: Sequence[str], start: datetime, end: datetime) -> list[Path]:
        del start, end
        resolve_calls.append(tuple(groups))
        return [path]

    async def live_stream(groups: Sequence[str], filter_pattern: str | None) -> AsyncIterator[LogEvent]:
        live_calls.append((tuple(groups), filter_pattern))
        for event in _make_live_events(2):
            yield event

    session = _session(filter_pattern='ERROR', selected_groups=['/aws/test/a', '/aws/test/b'])
    app = _make_app(
        ['/aws/test/a', '/aws/test/b'],
        services=ShellServices(resolve_logs=resolve, live_stream=live_stream),
        session=session,
    )

    async with running(app) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        screen = _logs_screen(app)

        assert resolve_calls == [('/aws/test/a', '/aws/test/b')]

        await pilot.press('L')
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert screen.live_mode
        assert live_calls == [(('/aws/test/a', '/aws/test/b'), 'ERROR')]

        await pilot.press('L')
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert screen._live_mode is False
        assert screen._live_active is False
        assert len(resolve_calls) == 2
        assert resolve_calls[1] == ('/aws/test/a', '/aws/test/b')
        assert app.session.filter_pattern == 'ERROR'


@pytest.mark.asyncio
async def test_live_toggle_without_service_notifies():
    app = _make_app()

    async with running(app) as pilot:
        screen = _logs_screen(app)
        await pilot.press('L')
        await pilot.pause()

        assert screen.live_mode is False


@pytest.mark.asyncio
async def test_refresh_while_live_is_a_no_op():
    app = _make_app(live=True, services=ShellServices(live_stream=streams(())))

    async with running(app) as pilot:
        screen = _logs_screen(app)
        await pilot.press('r')
        await pilot.pause()

        assert screen.live_mode is True


@pytest.mark.asyncio
async def test_live_refresh_view_restarts_stream():
    live_calls: list[str | None] = []

    async def live_stream(groups: Sequence[str], filter_pattern: str | None) -> AsyncIterator[LogEvent]:
        del groups
        live_calls.append(filter_pattern)
        for event in _make_live_events(0):
            yield event

    app = _make_app(live=True, services=ShellServices(live_stream=live_stream))

    async with running(app) as pilot:
        await app.workers.wait_for_complete()
        screen = _logs_screen(app)
        app.session.filter_pattern = 'timeout'
        screen.refresh_view()
        await app.workers.wait_for_complete()
        await pilot.pause()

    assert live_calls == [None, 'timeout']


@pytest.mark.asyncio
async def test_restore_focus_returns_to_table():
    app = _make_app()

    async with running(app) as pilot:
        screen = _logs_screen(app)
        assert screen._search_input is not None
        screen._search_input.focus()
        await pilot.pause()

        screen.restore_focus()
        await pilot.pause()

        assert app.screen.query_one('#log_table', DataTable).has_focus


@pytest.mark.asyncio
async def test_toggle_trace_view(tmp_path: Path):
    parquet_path = tmp_path / 'logs.parquet'
    _create_parquet_with_traces(parquet_path, trace_count=2)
    app = _make_app(services=_resolving_to([parquet_path]))

    async with running(app) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press('t')
        await pilot.pause()

        assert isinstance(app.screen, TraceViewerScreen)


@pytest.mark.asyncio
async def test_opening_on_a_trace_id_lands_in_the_trace_view(tmp_path: Path):
    """`:trace <id>` opens the log window and goes straight to that trace."""
    parquet_path = tmp_path / 'logs.parquet'
    trace_ids = _create_parquet_with_traces(parquet_path, trace_count=2)
    app = _make_app(services=_resolving_to([parquet_path]), trace_id=trace_ids[0])

    async with running(app) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert isinstance(app.screen, TraceViewerScreen)


@pytest.mark.asyncio
async def test_toggle_trace_view_no_data():
    app = _make_app()

    async with running(app) as pilot:
        await pilot.pause()
        await pilot.press('t')
        await pilot.pause()

        assert not isinstance(app.screen, TraceViewerScreen)


@pytest.mark.asyncio
async def test_show_trace_for_selected(tmp_path: Path):
    parquet_path = tmp_path / 'logs.parquet'
    _create_parquet_with_traces(parquet_path, trace_count=2, spans_per_trace=3)
    app = _make_app(services=_resolving_to([parquet_path]))

    async with running(app) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()

        table = app.screen.query_one('#log_table', DataTable)
        assert table.row_count > 0
        table.move_cursor(row=0)
        await pilot.pause()

        await pilot.press('shift+t')
        await pilot.pause()

        assert isinstance(app.screen, TraceViewerScreen)


@pytest.mark.asyncio
async def test_show_trace_for_selected_no_trace_id(tmp_path: Path):
    events = [
        LogEvent(
            message='Plain message',
            timestamp=BASE_TIME + timedelta(seconds=index),
            ingestion_time=BASE_TIME,
            log_stream='test-stream',
            log_group='test-group',
        )
        for index in range(3)
    ]
    parquet_path = _write_parquet(events, tmp_path / 'logs.parquet')
    app = _make_app(services=_resolving_to([parquet_path]))

    async with running(app) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()

        table = app.screen.query_one('#log_table', DataTable)
        table.move_cursor(row=0)
        await pilot.pause()
        await pilot.press('shift+t')
        await pilot.pause()

        assert not isinstance(app.screen, TraceViewerScreen)


@pytest.mark.asyncio
async def test_trace_view_back_to_logs(tmp_path: Path):
    parquet_path = tmp_path / 'logs.parquet'
    _create_parquet_with_traces(parquet_path, trace_count=1)
    app = _make_app(services=_resolving_to([parquet_path]))

    async with running(app) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press('t')
        await pilot.pause()
        assert isinstance(app.screen, TraceViewerScreen)

        await pilot.press('escape')
        await pilot.pause()

        assert not isinstance(app.screen, TraceViewerScreen)


@pytest.mark.asyncio
async def test_trace_view_empty_results(tmp_path: Path):
    events = [
        LogEvent(
            message='Plain message',
            timestamp=BASE_TIME,
            ingestion_time=BASE_TIME,
            log_stream='test-stream',
            log_group='test-group',
        ),
    ]
    parquet_path = _write_parquet(events, tmp_path / 'logs.parquet')
    app = _make_app(services=_resolving_to([parquet_path]))

    async with running(app) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press('t')
        await pilot.pause()

        assert not isinstance(app.screen, TraceViewerScreen)
        assert 'No traces found' in str(app.screen.query_one('#status', Label).render())


@pytest.mark.asyncio
async def test_trace_view_with_errors(tmp_path: Path):
    base_time = datetime.now(UTC)
    events = [
        LogEvent(
            message=json.dumps(
                {
                    'trace_id': 'trace-error',
                    'service_name': 'test-service',
                    'level': 'ERROR' if index == 1 else 'INFO',
                },
            ),
            timestamp=base_time + timedelta(seconds=index),
            ingestion_time=base_time,
            log_stream='test-stream',
            log_group='test-group',
        )
        for index in range(3)
    ]
    parquet_path = _write_parquet(events, tmp_path / 'logs.parquet')
    app = _make_app(services=_resolving_to([parquet_path]))

    async with running(app) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press('t')
        await pilot.pause()

        assert isinstance(app.screen, TraceViewerScreen)
        assert len(app.screen._trace_groups) == 1
        assert app.screen._trace_groups[0].error_count == 1


@pytest.mark.asyncio
async def test_trace_view_multiple_toggles(tmp_path: Path):
    parquet_path = tmp_path / 'logs.parquet'
    _create_parquet_with_traces(parquet_path, trace_count=1)
    app = _make_app(services=_resolving_to([parquet_path]))

    async with running(app) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press('t')
        await pilot.pause()
        await pilot.press('escape')
        await pilot.pause()
        await pilot.press('t')
        await pilot.pause()

        assert isinstance(app.screen, TraceViewerScreen)
        assert len(app.screen._trace_groups) > 0


def test_bindings_include_trace_and_live_shortcuts():
    keys = {binding.key for binding in LogsScreen.BINDINGS}

    assert {'t', 'shift+t', 'L', '/', 'r', 'space', 'enter'} <= keys
    assert 'q' not in keys


@pytest.mark.asyncio
async def test_trace_view_performance(tmp_path: Path):
    parquet_path = tmp_path / 'logs.parquet'
    _create_parquet_with_traces(parquet_path, trace_count=50, spans_per_trace=10)
    app = _make_app(services=_resolving_to([parquet_path]))

    async with running(app) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press('t')
        await pilot.pause()

        assert isinstance(app.screen, TraceViewerScreen)
        assert len(app.screen._trace_groups) == 50


@pytest.mark.asyncio
async def test_the_search_box_only_takes_room_while_it_is_in_use():
    """Three of 24 rows is a lot to spend on an empty input."""
    app = _make_app()

    async with running(app) as pilot:
        screen = _logs_screen(app)
        screen.load_events(_make_test_log_events(3))
        await pilot.pause()
        assert _search_shown(screen) is False

        await pilot.press('/')
        await pilot.pause()
        assert _search_shown(screen) is True

        await pilot.press('escape')
        await pilot.pause()

        assert _search_shown(screen) is False, 'escape closes the search rather than leaving the view'
        assert len(app.nav.stack) == 1, 'and it does not pop the view out from under it'


async def test_p_pivots_the_row_onto_its_own_correlation_id(tmp_path):
    """The pivot lands in the search box so it is visible, editable, and clearable."""
    events = [
        _json_event('start', offset=0),
        LogEvent(
            timestamp=BASE_TIME + timedelta(seconds=1),
            message=json.dumps({'trace_id': '1-6a89adb9-b279784d231d504c96c8815f', 'event': 'slow'}),
            log_group=DEFAULT_GROUP,
            log_stream='stream-1',
            ingestion_time=None,
        ),
    ]
    path = tmp_path / 'events.parquet'
    write_log_events_to_parquet(events, path)
    app = _make_app(services=_resolving_to([path]))

    async with running(app, settled=True) as pilot:
        screen = _logs_screen(app)
        table = app.screen.query_one('#log_table', DataTable)
        table.focus()
        table.move_cursor(row=1)
        await pilot.press('p')
        await pilot.pause()

        assert screen._search_input is not None
        assert screen._search_input.value == 'trace_id:1-6a89adb9-b279784d231d504c96c8815f'
        assert screen._search_input.display, 'a pivot the user cannot see is a pivot they cannot undo'


async def test_x_opens_the_waterfall_only_for_an_id_x_ray_can_answer_for(tmp_path):
    """Both sides of the stack log a trace_id and only one generates X-Ray ids."""
    # Built from now, not hardcoded: the id's own epoch is what marks it as X-Ray's, so a
    # fixed one stops being one 32 days after it is written.
    xray_id = f'1-{int(datetime.now(UTC).timestamp()):08x}-b279784d231d504c96c8815f'
    events = [
        LogEvent(
            timestamp=BASE_TIME + timedelta(seconds=index),
            message=json.dumps({'trace_id': trace_id, 'event': 'e'}),
            log_group=DEFAULT_GROUP,
            log_stream='s',
            ingestion_time=None,
        )
        for index, trace_id in enumerate(('7f3c1e9a4b2d', xray_id))
    ]
    path = tmp_path / 'events.parquet'
    write_log_events_to_parquet(events, path)
    app = _make_app(services=_resolving_to([path]))

    async with running(app, settled=True) as pilot:
        table = app.screen.query_one('#log_table', DataTable)
        table.focus()
        table.move_cursor(row=0)
        await pilot.press('x')
        await pilot.pause()
        assert isinstance(app.screen, LogsScreen), 'a worker-side id is not an X-Ray id'

        table.move_cursor(row=1)
        await pilot.press('x')
        await pilot.pause()

        assert app.screen.__class__.__name__ == 'WaterfallScreen'
        assert app.nav.stack[-1].argument == xray_id


async def test_h_shows_when_the_events_happened_and_marks_a_capped_load(tmp_path):
    """A binding is not covered until a test presses the key."""
    session = _session(start=BASE_TIME, end=BASE_TIME + timedelta(minutes=10))
    events = [_json_event(f'e{index}', offset=index) for index in range(4)]
    path = tmp_path / 'events.parquet'
    write_log_events_to_parquet(events, path)
    app = _make_app(services=_resolving_to([path]), session=session)

    async with running(app, settled=True) as pilot:
        screen = _logs_screen(app)
        row = app.screen.query_one('#histogram', Label)
        assert not row.has_class('shown'), 'the row stays out of the way until asked for'

        await pilot.press('h')
        await pilot.pause()

        assert row.has_class('shown')
        assert 'peak 4 at 10:00:00' in str(row.render())
        assert 'capped' not in str(row.render())

        screen._load_capped = True
        screen._draw_histogram()
        await pilot.pause()
        assert 'capped, not the whole window' in str(row.render()), 'the shape of a capped load is the shape of the cap'

        await pilot.press('h')
        await pilot.pause()
        assert not row.has_class('shown')


def _facet_services(paths: Sequence[Path]) -> ShellServices:
    """Resolve to ``paths``, and count their fields the way the live service does."""

    async def resolve(groups: Sequence[str], start: datetime, end: datetime) -> list[Path]:
        del groups, start, end
        return list(paths)

    async def facets(files: Sequence[Path], filter_pattern: str | None, top: int) -> list[FieldFacet]:
        filter_node = parse_query(filter_pattern) if filter_pattern else None
        fields = discover_field_paths(list(files), limit=8)
        return worth_showing([count_by_field(list(files), field, filter_node=filter_node, top=top) for field in fields])

    return ShellServices(resolve_logs=resolve, field_facets=facets)


async def _settle(app: TailCWApp, pilot: Pilot[None]) -> None:
    """Wait out the load, then the field counts the load starts."""
    for _ in range(4):
        await app.workers.wait_for_complete()
        await pilot.pause()


def _facet_app(tmp_path: Path, messages: list[str]) -> TailCWApp:
    """A log view whose one cached file holds exactly these messages."""
    events = [
        LogEvent(
            timestamp=BASE_TIME + timedelta(seconds=index),
            message=message,
            log_group=DEFAULT_GROUP,
            log_stream='stream-0',
            ingestion_time=None,
        )
        for index, message in enumerate(messages)
    ]
    return _make_app(services=_facet_services([_write_parquet(events, tmp_path / 'facets.parquet')]))


@pytest.mark.asyncio
async def test_the_field_panel_counts_what_the_window_holds(tmp_path: Path):
    """Half the log view was empty, and knowing the payload schema was a prerequisite."""
    app = _facet_app(tmp_path, ['{"level":"info"}'] * 3 + ['{"level":"error"}'])

    async with app.run_test(size=(120, 30)) as pilot:
        await _settle(app, pilot)
        panel = app.screen.query_one('#facets', FacetsPanel)

        assert panel.has_class('shown')
        prompts = [str(option.prompt) for option in panel._options]
        assert any(prompt.strip() == 'level' for prompt in prompts)
        assert any('info' in prompt and '3' in prompt for prompt in prompts)


@pytest.mark.asyncio
async def test_a_narrow_terminal_gives_the_panels_cells_to_the_message(tmp_path: Path):
    """Below the threshold the message column is the thing that needs them."""
    app = _facet_app(tmp_path, ['{"level":"info"}'])

    async with app.run_test(size=(80, 24)) as pilot:
        await _settle(app, pilot)

        assert not app.screen.query_one('#facets', FacetsPanel).has_class('shown')


@pytest.mark.asyncio
async def test_pressing_f_moves_between_the_table_and_the_field_panel(tmp_path: Path):
    """A binding is not covered until a test presses the key."""
    app = _facet_app(tmp_path, ['{"level":"info"}'])

    async with app.run_test(size=(120, 30)) as pilot:
        await _settle(app, pilot)
        panel = app.screen.query_one('#facets', FacetsPanel)

        await pilot.press('f')
        await pilot.pause()
        assert panel.has_focus

        await pilot.press('f')
        await pilot.pause()
        assert app.screen.query_one('#log_table', DataTable).has_focus


@pytest.mark.asyncio
async def test_choosing_a_field_value_filters_the_table(tmp_path: Path):
    app = _facet_app(tmp_path, ['{"level":"info"}'] * 3 + ['{"level":"error"}'])

    async with app.run_test(size=(120, 30)) as pilot:
        await _settle(app, pilot)
        screen = _logs_screen(app)
        assert screen.query_one('#log_table', DataTable).row_count == 4

        await pilot.press('f')
        await pilot.pause()
        await pilot.press('down', 'enter')
        await _settle(app, pilot)

        assert screen._search_input is not None
        assert screen._search_input.value == 'level:info'


@pytest.mark.asyncio
async def test_hiding_the_panel_gives_its_width_back_to_the_message(tmp_path: Path):
    app = _facet_app(tmp_path, ['{"level":"info"}'])

    async with app.run_test(size=(120, 30)) as pilot:
        await _settle(app, pilot)
        screen = _logs_screen(app)
        narrow = next(column.width for column in screen._columns if column.key == 'message')

        screen.run_view_command('fields', '')
        await _settle(app, pilot)

        wide = next(column.width for column in screen._columns if column.key == 'message')
        assert wide > narrow
