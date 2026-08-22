"""The log view: a historical window and a live stream behind one toggle.

One screen owns both halves on purpose. Flipping ``L`` swaps the data source
while the group selection, filter, and window stay on the shared session, so a
search you like becomes a tail without retyping it. Every AWS touch arrives as
a callable on ``ShellServices``; the screen itself reads Parquet and renders.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from time import monotonic
from typing import Any, ClassVar

from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.message import Message
from textual.timer import Timer
from textual.widgets import DataTable, Input, Label
from textual.worker import get_current_worker

from tail_cw.aws.events import LogEvent
from tail_cw.aws.xray import as_xray_trace_id
from tail_cw.charts.sparkline import sparkline_blocks
from tail_cw.cli import server_side_pattern
from tail_cw.concurrency import closing_stream
from tail_cw.config import TailCWConfig
from tail_cw.histogram import bucket_events, histogram_headline
from tail_cw.query.engine import query_parquet_files_to_log_events
from tail_cw.query.expression import parse_query
from tail_cw.query.parser import FilterNode, combine_filters, parse_filter_pattern
from tail_cw.query.severity import Severity
from tail_cw.query.trace import (
    TraceGroup,
    correlation_ids,
    extract_trace_id_from_event,
    query_traces_from_parquet_files,
)
from tail_cw.tui.command_bar import SearchLine
from tail_cw.tui.log_viewer import Column, format_rows, plan_columns
from tail_cw.tui.navigation import NavTarget, ViewKind
from tail_cw.tui.record_detail import RecordDetailScreen
from tail_cw.tui.shell import MAX_LABEL_CHARS, ResolveLogs, ShellCommand, ShellScreen
from tail_cw.tui.trace_viewer import TraceViewerScreen

LiveStreamFactory = Callable[[], AsyncIterator[LogEvent]]

_LIVE_FLUSH_INTERVAL_SECONDS = 0.25
_LOAD_TICK_SECONDS = 1.0
_PIVOT_FIELDS_NAMED = 3
_HISTOGRAM_MARGIN = 52
"""Columns the headline and the capped note keep beside the bars."""
_HISTOGRAM_MIN_COLUMNS = 8
_HISTOGRAM_COLORS = {Severity.INFO: 'cyan', Severity.WARNING: 'yellow', Severity.ERROR: 'red'}
_LOADING_STATUS = 'Loading events, esc to stop'
_HALF_PAGE = 10
_ROW_RESERVE = 4
"""Cells the elastic column cannot use: the cursor gutter and the scrollbar.

Without it the message is cut at the pane edge and the ellipsis marking the cut
is itself off-screen, which is the clipping the column budget exists to avoid.
"""


@dataclass(slots=True, init=False)
class ProgressUpdate(Message):
    """Message dispatched by background workers to report progress.

    Attributes:
        current: Current progress value (items processed).
        total: Total number of items when known, otherwise ``-1``.
        status: Human-readable status message.
    """

    current: int
    total: int
    status: str

    def __init__(self, current: int, total: int, status: str) -> None:
        """Store progress metadata and initialise the message."""
        self.current = current
        self.total = total
        self.status = status
        Message.__init__(self)


def _field_syntax_hint(query: str) -> str:
    """Suggest the syntax a fruitless search looks like it meant.

    Two searches parse cleanly and can never match. ``key=value`` is what the table
    renders, so it is what gets typed, but the field operator is ``:`` and ``=`` falls
    through to a text match no JSON record satisfies. And ``/re/`` is a text search for
    those slashes, because the regex delimiter here is ``%``.
    """
    stripped = query.strip()
    if len(stripped) > 1 and stripped.startswith('/') and stripped.endswith('/'):
        return f' · try %{stripped[1:-1]}% for a regex'
    field, separator, value = query.partition('=')
    if not (separator and field and value) or any(character in query for character in ' \t:'):
        return ''
    return f' · try {field}:{value} to match the record field'


class LogsScreen(ShellScreen):  # ruff: ignore[too-many-public-methods]
    """Browse log events for the selected groups, historical or live.

    Keyboard shortcuts:
        - /: Focus the search input
        - Enter: Show the detail modal for the selected row
        - r: Re-read the window with its end extended to now
        - L: Toggle between the historical window and a live stream
        - Space: Pause or resume live rendering
        - t: Trace view over every trace in the data
        - T: Trace view for the selected event
    """

    DEFAULT_CSS = """
    LogsScreen {
        layout: vertical;
    }

    #log_table {
        height: 1fr;
        width: 100%;
    }

    #histogram {
        height: 1;
        width: 100%;
        display: none;
    }

    #histogram.shown {
        display: block;
    }

    #status {
        height: 1;
        width: 100%;
        background: $panel;
        color: $text;
        padding: 0 1;
        text-align: center;
    }

    Container {
        height: 1fr;
        layout: vertical;
    }
    """

    BINDINGS: ClassVar[Sequence[Binding]] = [
        Binding('/', 'focus_search', 'Search', show=True),
        Binding('enter', 'show_detail', 'Detail', show=True),
        Binding('r', 'refresh', 'Refresh', show=True),
        Binding('L', 'toggle_live', 'Live', show=True),
        Binding('space', 'toggle_live_pause', 'Pause/Resume', show=False),
        Binding('t', 'toggle_trace_view', 'Trace View', show=True),
        Binding('shift+t', 'show_trace_for_selected', 'Show Trace', show=True),
        Binding('x', 'pivot_xray', 'X-Ray', show=True),
        Binding('p', 'pivot', 'Pivot', show=True),
        Binding('h', 'toggle_histogram', 'When', show=True),
        # DataTable answers to the arrow keys; these are the vim motions over the
        # same cursor, so hjkl-hands never reach for the arrows.
        Binding('j', 'move(1)', 'Down', show=False),
        Binding('k', 'move(-1)', 'Up', show=False),
        Binding('ctrl+d', f'move({_HALF_PAGE})', 'Half page down', show=False),
        Binding('ctrl+u', f'move(-{_HALF_PAGE})', 'Half page up', show=False),
        Binding('g', 'jump_top', 'Top', show=False),
        Binding('G', 'jump_bottom', 'Bottom', show=False),
    ]

    def __init__(self, log_groups: Sequence[str], *, live: bool = False, trace_id: str | None = None) -> None:
        """Open the view over the given groups, streaming when ``live`` is set.

        ``trace_id`` opens the trace view over that id as soon as the window is
        read, which is how an id pasted out of an alarm reaches a trace.
        """
        super().__init__()
        self._log_groups: list[str] = list(log_groups)
        self._live_mode = live
        self._pending_trace_id = trace_id
        self._log_events: list[LogEvent] = []
        self._all_events: list[LogEvent] = []
        self._table: DataTable[Any] | None = None
        self._columns: tuple[Column, ...] = ()
        self._search_input: SearchLine | None = None
        self._parquet_paths: list[Path] = []
        self._trace_id_fields: list[str] = []
        self._live_stream_factory: LiveStreamFactory | None = None
        self._live_buffer: deque[LogEvent] = deque()
        self._pending_live_events: deque[LogEvent] = deque()
        self._live_flush_timer: Timer | None = None
        self._live_active = False
        self._live_paused = False
        self._live_sampled = False
        self._live_event_count = 0
        self._show_histogram = False
        self._load_capped = False
        self._load_timer: Timer | None = None
        self._loading_since: float | None = None

    @property
    def _config(self) -> TailCWConfig:
        return self.shell.config_data

    @property
    def log_groups(self) -> list[str]:
        """The groups this view reads, in selection order."""
        return list(self._log_groups)

    @property
    def live_mode(self) -> bool:
        """Whether the view is streaming rather than reading a window."""
        return self._live_mode

    def compose_content(self) -> ComposeResult:  # ruff: ignore[no-self-use]
        """Build the view's own widgets.

        Yields:
            The search input, the table inside its container, and the status label.
        """
        yield SearchLine(placeholder='Search (CloudWatch syntax or key:value)...')
        yield Label('', id='histogram')
        with Container():
            yield DataTable(
                id='log_table',
                show_header=True,
                show_cursor=True,
                zebra_stripes=True,
                cursor_type='row',
            )
            yield Label('No logs loaded', id='status')

    def on_mount(self) -> None:
        """Wire up the table, then load either the window or the live stream."""
        super().on_mount()
        self._table = self.query_one('#log_table', DataTable)
        self._search_input = self.query_one(SearchLine)
        self._trace_id_fields = list(self._config.trace.trace_id_fields)
        self._live_buffer = deque(maxlen=self._config.tui.live_buffer_limit)
        self._setup_table_columns()

        if self._live_mode:
            self._start_live()
        else:
            self._load_window()

        self._table.focus()

    def commands(self) -> dict[str, ShellCommand]:  # ruff: ignore[no-self-use]
        """Add the log-specific ``:`` commands."""
        return {
            'live': ShellCommand('Toggle between the historical window and a live stream'),
            'trace': ShellCommand('Open the trace view, over one id or the whole window', ('<trace>',)),
            'pivot': ShellCommand("Search every selected group for the row's own id", ('<field>',)),
        }

    def run_view_command(self, name: str, argument: str) -> bool:
        """Run ``:live`` or ``:trace``, leaving anything else to the shell."""
        match name:
            case 'live':
                self.action_toggle_live()
            case 'trace':
                self.show_trace(argument.strip() or None)
            case 'pivot':
                self.action_pivot(argument.strip())
            case _:
                return False
        return True

    def show_trace(self, trace_id: str | None) -> None:
        """Open the trace view over one id, or over every trace in the window."""
        if not self._parquet_paths:
            self.notify('Trace view requires Parquet data source', severity='warning')
            return
        self.run_worker(self._open_trace_view(trace_id), name='traces', group='traces', exclusive=True)

    def _open_pending_trace(self) -> None:
        if self._pending_trace_id is None:
            return
        trace_id, self._pending_trace_id = self._pending_trace_id, None
        self.show_trace(trace_id)

    def nav_siblings(self) -> list[NavTarget]:
        """One target per selected group, so ``[`` and ``]`` cycle groups.

        A merged multi-group view is not itself one of the per-group targets, so
        it leads the list and ``]`` steps from the merge into the first group.
        """
        pool = self.shell.session.selected_groups or self._log_groups
        prefix = 'tail' if self._live_mode else 'logs'
        targets = [NavTarget(kind=ViewKind.LOGS, label=f'{prefix} {group}', payload=(group,)) for group in pool]
        current = self.shell.nav.stack[-1]
        if current not in targets:
            targets.insert(0, current)
        return targets

    def refresh_view(self) -> None:
        """Re-read the source after the shared window or filter changed."""
        if self._live_mode:
            self._stop_live()
            self._start_live()
        else:
            self._load_window()

    def restore_focus(self) -> None:
        """Send focus back to the table."""
        if self._table is not None:
            self._table.focus()

    def _setup_table_columns(self) -> None:
        """Give the table the columns this width can afford, replacing any it had."""
        if self._table is None:
            return
        self._columns = self._plan()
        self._table.clear(columns=True)
        for column in self._columns:
            self._table.add_column(column.label, key=column.key, width=column.width)

    def _plan(self) -> tuple[Column, ...]:
        return plan_columns(self._table_width() - _ROW_RESERVE, single_group=len(self._log_groups) <= 1)

    def _table_width(self) -> int:
        """Width the column budget is planned against.

        The screen's width rather than the table's: the table measures 0 until
        its first layout, so planning from it would re-plan and rebuild every
        row the moment the real size arrived.
        """
        return self.app.size.width

    def _rows(self, events: Sequence[LogEvent]) -> list[tuple[Any, ...]]:
        return format_rows(events, self._columns, self._config.message)

    def on_resize(self, _event: events.Resize) -> None:
        """Re-budget the columns when the terminal changes size.

        Only a plan that actually differs rebuilds the table, because rebuilding
        a thousand rows on every intermediate width of a drag is visible.
        """
        if self._table is None:
            return
        planned = self._plan()
        if planned == self._columns:
            return
        self._setup_table_columns()
        self._load_log_events()

    def action_move(self, offset: int) -> None:
        """Move the row cursor, which is what j, k, and the half-page keys drive."""
        if self._table is None:
            return
        self._table.move_cursor(row=max(0, min(self._table.cursor_row + offset, self._table.row_count - 1)))

    def action_jump_top(self) -> None:
        """Jump to the first row."""
        if self._table is not None:
            self._table.move_cursor(row=0)

    def action_jump_bottom(self) -> None:
        """Jump to the last row."""
        if self._table is not None:
            self._table.move_cursor(row=max(0, self._table.row_count - 1))

    def _load_window(self) -> None:
        resolve = self.shell.services.resolve_logs
        if resolve is None:
            self._update_status('No log source available')
            return
        self._start_loading_clock()
        session = self.shell.session
        self.run_worker(
            self._resolve_window(resolve, session.start, session.end),
            name='resolve_logs',
            group='resolve_logs',
            exclusive=True,
        )

    def _start_loading_clock(self) -> None:
        """Count the wait out loud, and say how to leave.

        A cold multi-group window takes tens of seconds, and a status line that
        says only "Loading" for that long is indistinguishable from a hang. Escape
        pops the screen, which closes it and cancels this worker with it.
        """
        self._stop_loading_clock()
        self._loading_since = monotonic()
        self._update_status(_LOADING_STATUS)
        self._load_timer = self.set_interval(_LOAD_TICK_SECONDS, self._tick_loading_clock)

    def _tick_loading_clock(self) -> None:
        if self._loading_since is None:
            return
        elapsed = int(monotonic() - self._loading_since)
        self._update_status(f'{_LOADING_STATUS} · {elapsed}s')

    def action_nav_pop(self) -> None:
        """Stop an in-flight load; a second press goes back.

        The log view is reachable as the opening view, where there is no screen
        to pop, so a cold multi-group fetch had no way out at all.
        """
        if self._loading_since is None:
            super().action_nav_pop()
            return
        self.workers.cancel_group(self, 'resolve_logs')
        self._stop_loading_clock()
        self._update_status('Load stopped, r to try again')

    def _stop_loading_clock(self) -> None:
        if self._load_timer is not None:
            self._load_timer.stop()
            self._load_timer = None
        self._loading_since = None

    async def _resolve_window(self, resolve: ResolveLogs, start: datetime, end: datetime) -> None:
        try:
            paths = await resolve(tuple(self._log_groups), start, end)
        except Exception as err:
            self._stop_loading_clock()
            self.notify(f'Failed to load logs: {err}', severity='error')
            self._update_status(f'Load error: {err}')
            return
        self._stop_loading_clock()
        self.set_parquet_sources(paths)

    def set_parquet_sources(self, paths: Sequence[Path]) -> None:
        """Read the resolved Parquet files as the view's data source.

        Several files merge by timestamp so a multi-group search reads as one
        stream while each group keeps its own cached file.

        Args:
            paths: One Parquet file per log group; an empty list clears the view.
        """
        self._parquet_paths = [path for path in paths if path.exists()]
        if not self._parquet_paths:
            self._log_events = []
            self._all_events = []
            self._load_log_events([])
            self._update_status('No events found')
            return

        initial_limit = self._config.tui.initial_load_limit
        try:
            events = list(
                query_parquet_files_to_log_events(self._parquet_paths, self._session_filter(), limit=initial_limit),
            )
        except Exception as err:
            self.notify(f'Failed to load Parquet file: {err}', severity='error')
            self._update_status(f'Error loading Parquet: {err}')
            return

        self._log_events = events
        self._all_events = events.copy()
        self._load_log_events(events)
        self._load_capped = len(events) >= initial_limit
        hint = ' · capped, narrow the window or add a filter' if self._load_capped else ''
        self._update_status(f'Loaded {len(events):,} events{hint}')
        self._open_pending_trace()

    def load_events(self, events: list[LogEvent], parquet_paths: Sequence[Path] | None = None) -> None:
        """Replace the displayed events, optionally pointing search at new files."""
        self._log_events = events
        self._all_events = events.copy()
        self._load_log_events(events)

        if parquet_paths is not None:
            self._parquet_paths = list(parquet_paths)

        self._update_status(f'Loaded {len(events)} events')

    def _load_log_events(self, events: list[LogEvent] | None = None) -> None:
        if self._table is None:
            return

        events_to_load = events if events is not None else self._log_events
        self._table.clear(columns=False)
        self._table.loading = True
        self._draw_histogram()

        if len(events_to_load) > self._config.tui.chunk_threshold:
            self.run_worker(
                self._load_events_incrementally(events_to_load, self._config.tui.chunk_size),
                name='load_events',
                exclusive=True,
            )
        else:
            self._table.add_rows(self._rows(events_to_load))
            self._table.loading = False

    async def _load_events_incrementally(self, events: list[LogEvent], chunk_size: int) -> None:
        worker = get_current_worker()
        total = len(events)

        for start_idx in range(0, total, chunk_size):
            if worker.is_cancelled:
                break
            end_idx = min(start_idx + chunk_size, total)
            chunk = events[start_idx:end_idx]
            if self._table is not None:
                self._table.add_rows(self._rows(chunk))
            self._post_progress(end_idx, total, 'Loading events')

        if self._table is not None:
            self._table.loading = False
        self._update_status(f'Loaded {total} events')

    def _post_progress(self, current: int, total: int, status: str) -> None:
        self.post_message(ProgressUpdate(current=current, total=total, status=status))

    def _update_status(self, message: str) -> None:
        """Show one line of plain text, never markup.

        Error text is not ours: every Polars failure names the file it failed on
        as ``[/path/to.parquet]``, which Rich reads as a closing tag and raises
        ``MarkupError`` from inside the update. That took the whole app down on
        any failed search, so the error handler was worse than the error.
        """
        self.query_one('#status', Label).update(Text(message))

    def on_progress_update(self, message: ProgressUpdate) -> None:
        """Show a worker's progress on the status line."""
        if message.total > 0:
            status_text = f'{message.status} ({message.current}/{message.total})'
        else:
            status_text = f'{message.status} ({message.current} events)'
        self._update_status(status_text)
        message.stop()

    @on(DataTable.RowSelected, '#log_table')
    def on_row_selected(self, event: DataTable.RowSelected) -> None:
        """Open the detail modal for the row Enter was pressed on.

        DataTable binds Enter itself, so the screen's own binding never sees the
        key and the footer's Detail hint would otherwise do nothing.
        """
        event.stop()
        self.action_show_detail()

    def action_show_detail(self) -> None:
        """Open the detail modal for the row under the cursor."""
        event = self._selected_event()
        if event is not None:
            self.app.push_screen(RecordDetailScreen(event))

    def _selected_event(self) -> LogEvent | None:
        if self._table is None:
            return None
        row_index = self._table.cursor_row
        if row_index < 0 or not self._log_events:
            return None
        try:
            return self._log_events[row_index]
        except IndexError:
            self.notify('Invalid row selection', severity='error')
            return None

    def action_focus_search(self) -> None:
        """Focus the search input, or say why search is unavailable."""
        if self._search_input is None:
            return
        if not self._parquet_paths and not self._all_events:
            self.notify('No data source available for search', severity='warning')
            return
        self._search_input.open()

    def action_refresh(self) -> None:
        """Extend the shared window to now and re-read it."""
        if self._live_mode:
            self.notify('Live tail is already following new events', severity='information')
            return
        self.shell.set_window(self.shell.session.start, datetime.now(tz=UTC))

    def action_toggle_live(self) -> None:
        """Flip between the historical window and a live stream.

        The groups, filter, and window all live on the session, so the flip
        changes only where events come from.
        """
        if self._live_mode:
            self._stop_live()
            self._live_mode = False
            self._load_window()
            self.notify('Live tail stopped; showing the historical window', severity='information')
            return
        if self.shell.services.live_stream is None:
            self.notify('Live tail is unavailable without a stream service', severity='warning')
            return
        self._live_mode = True
        self._start_live()

    def _start_live(self) -> None:
        stream = self.shell.services.live_stream
        if stream is None:
            self._update_status('Live tail unavailable')
            return
        try:
            # CloudWatch applies this one, so a local-only expression has to be refused
            # rather than sent: it would silently drop its own any-of terms.
            sent_pattern = server_side_pattern(self.shell.session.filter_pattern)
        except ValueError as err:
            self._update_status(f'Live tail cannot use this filter: {err}')
            return
        self._log_events = []
        self._all_events = []
        self._load_log_events([])
        self.start_live_tail(partial(stream, tuple(self._log_groups), sent_pattern))

    def _stop_live(self) -> None:
        self.workers.cancel_group(self, 'live_tail')
        if self._live_flush_timer is not None:
            self._live_flush_timer.stop()
            self._live_flush_timer = None
        self._live_stream_factory = None
        self._live_active = False
        self._live_paused = False
        self._live_event_count = 0
        self._live_buffer.clear()
        self._pending_live_events.clear()

    def start_live_tail(self, stream_factory: LiveStreamFactory) -> None:
        """Stream events from the given factory into a bounded ring buffer.

        The factory is invoked once, on the message loop. Events are coalesced
        and rendered in batches; call this after the screen mounts.
        """
        self._live_stream_factory = stream_factory
        self._live_buffer = deque(maxlen=self._config.tui.live_buffer_limit)
        if self._table is not None:
            self._begin_live_tail()

    def _begin_live_tail(self) -> None:
        if self._live_stream_factory is None or self._live_active:
            return
        self._live_active = True
        self._update_live_status()
        self._live_flush_timer = self.set_interval(_LIVE_FLUSH_INTERVAL_SECONDS, self._flush_live_events)
        self.run_worker(
            self._consume_live_stream(),
            name='live_tail',
            group='live_tail',
            exclusive=True,
        )

    async def _consume_live_stream(self) -> None:
        if self._live_stream_factory is None:
            return
        try:
            async with closing_stream(self._live_stream_factory()) as stream:
                async for event in stream:
                    self._pending_live_events.append(event)
        except asyncio.CancelledError:
            raise
        except Exception as err:
            self._finish_live_tail(f'Live tail stopped: {err}')
            return
        self._finish_live_tail('Live tail stream ended')

    def _finish_live_tail(self, message: str) -> None:
        self._flush_live_events()
        self._live_active = False
        self.notify(message, severity='warning')
        self._update_live_status()

    def note_live_sampled(self, sampled: bool) -> None:  # ruff: ignore[boolean-type-hint-positional-argument]
        """Record whether the server is sampling the live stream."""
        self._live_sampled = sampled

    def _flush_live_events(self) -> None:
        drained: list[LogEvent] = []
        while self._pending_live_events:
            drained.append(self._pending_live_events.popleft())
        if drained:
            self._live_event_count += len(drained)
            self._live_buffer.extend(drained)
            self._all_events = list(self._live_buffer)
            if not self._live_paused and not self._live_search_active():
                self._render_live_batch(drained)
        if drained or self._live_active:
            self._update_live_status()

    def _render_live_batch(self, drained: list[LogEvent]) -> None:
        if self._table is None:
            return
        limit = self._live_buffer.maxlen or len(self._all_events)
        rebuild_slack = max(100, limit // 10)
        if self._table.row_count + len(drained) > limit + rebuild_slack:
            self._rebuild_live_table()
        else:
            self._log_events.extend(drained)
            self._table.add_rows(self._rows(drained))

    def _rebuild_live_table(self) -> None:
        if self._table is None:
            return
        self._log_events = list(self._live_buffer)
        self._table.clear(columns=False)
        self._table.add_rows(self._rows(self._log_events))

    def _live_search_active(self) -> bool:
        return self._search_input is not None and bool(self._search_input.value.strip())

    def action_toggle_live_pause(self) -> None:
        """Pause or resume rendering of new live events (the buffer keeps filling)."""
        if self._live_stream_factory is None:
            return
        self._live_paused = not self._live_paused
        if not self._live_paused and not self._live_search_active():
            self._rebuild_live_table()
        self._update_live_status()

    def _update_live_status(self) -> None:
        if self._live_paused:
            state = 'Paused'
        elif self._live_active:
            state = 'Live'
        else:
            state = 'Stopped'
        sampled = ' (sampled)' if self._live_sampled else ''
        limit = self._live_buffer.maxlen or 0
        self._update_status(
            f'{state}{sampled} · {self._live_event_count} events · buffer {len(self._live_buffer)}/{limit}',
        )

    def action_toggle_trace_view(self) -> None:
        """Open the trace view over every trace in the loaded data."""
        self.show_trace(None)

    def action_show_trace_for_selected(self) -> None:
        """Open the trace view for the selected event's trace."""
        if not self._parquet_paths:
            self.notify('Trace view requires Parquet data source', severity='warning')
            return

        log_event = self._selected_event()
        if log_event is None:
            self.notify('No row selected', severity='warning')
            return

        trace_id = extract_trace_id_from_event(log_event, self._trace_id_fields)
        if not trace_id:
            self.notify('No trace ID found in selected event', severity='information')
            return

        self.show_trace(trace_id)

    def action_toggle_histogram(self) -> None:
        """Show or hide when the events on screen actually happened."""
        self._show_histogram = not self._show_histogram
        self._draw_histogram()

    def _draw_histogram(self) -> None:
        """Redraw the histogram row from whatever the view is currently showing.

        Coloured per column by the worst severity in it, because a burst of errors and a
        burst of traffic are the same height and not the same finding.
        """
        row = self.query_one('#histogram', Label)
        row.set_class(self._show_histogram, 'shown')
        if not self._show_histogram:
            return
        # The screen's width, not the row's: a row that was hidden a moment ago has no
        # content region yet, which drew the whole window as one column.
        columns = self.size.width - _HISTOGRAM_MARGIN
        if columns < _HISTOGRAM_MIN_COLUMNS:
            row.update(Text('too narrow for a histogram'))
            return
        buckets = bucket_events(
            self._log_events,
            start=self.shell.session.start,
            end=self.shell.session.end,
            columns=columns,
        )
        if not buckets:
            row.update(Text('no window to bucket'))
            return
        blocks = sparkline_blocks([float(bucket.count) for bucket in buckets], width=columns, bars=True, lo=0.0)
        rendered = Text(no_wrap=True)
        for block, bucket in zip(blocks, buckets, strict=False):
            rendered.append(block, style=_HISTOGRAM_COLORS[bucket.severity])
        rendered.append(f' {histogram_headline(buckets)}', style='dim')
        if self._load_capped:
            # The shape of a capped load is the shape of the cap, not of the window: the
            # read stops at the limit, so every event sits at the window's near edge.
            rendered.append(' · capped, not the whole window', style='yellow')
        row.update(rendered)

    def action_pivot(self, field: str = '') -> None:
        """Search every selected group for the correlation id on the current row.

        The id goes into the search box rather than straight into a query, so the pivot
        is visible, editable, and undone by clearing it like any other search.
        """
        event = self._selected_event()
        if event is None:
            self.notify('No row selected', severity='warning')
            return
        wanted = [field] if field else list(self._trace_id_fields)
        found = correlation_ids(event, wanted)
        if not found:
            named = field or ', '.join(wanted[:_PIVOT_FIELDS_NAMED])
            self.notify(f'This row carries no {named}', severity='information')
            return
        name, value = found[0]
        if (search := self._search_input) is None:
            return
        search.open()
        search.value = f'{name}:{value}'

    def action_pivot_xray(self) -> None:
        """Open the X-Ray waterfall for the row's trace, when X-Ray can answer for it."""
        event = self._selected_event()
        if event is None:
            self.notify('No row selected', severity='warning')
            return
        trace_id = extract_trace_id_from_event(event, self._trace_id_fields)
        if not trace_id:
            self.notify('No trace id in the selected event', severity='information')
            return
        xray_id = as_xray_trace_id(trace_id, now=datetime.now(UTC))
        if xray_id is None:
            # Both sides of the stack log a trace_id and only one generates X-Ray ids.
            self.notify(f'{trace_id} is not an X-Ray id; T opens the log-derived trace', severity='warning')
            return
        self.shell.goto(
            NavTarget(kind=ViewKind.XRAY, label=f'xray {xray_id[:MAX_LABEL_CHARS]}', argument=xray_id),
        )

    async def _open_trace_view(self, trace_id: str | None) -> None:
        """Group the loaded Parquet windows into traces, then show them.

        Spans for one trace are spread across one file per log group, so every
        selected group is read together. The grouping is blocking DuckDB and
        Polars work, so it goes through the ``load_traces`` service onto the
        blocking pool rather than running here on the message loop.
        """
        label = f'trace {trace_id[:8]}' if trace_id else 'traces'
        self._update_status(f'Loading {label}...')

        try:
            trace_groups = await self._load_traces(trace_id)
        except Exception as err:
            self.notify(f'Failed to load {label}: {err}', severity='error')
            self._update_status(f'Trace loading error: {err}')
            return

        if not trace_groups:
            missing = f'Trace not found in current data: {trace_id}' if trace_id else 'No traces found in current data'
            self.notify(missing, severity='information')
            self._update_status('Trace not found' if trace_id else 'No traces found')
            return

        title = f'Trace: {trace_id[:16]}...' if trace_id else None
        self._update_status(f'{len(trace_groups)} traces across {len(self._parquet_paths)} groups')
        if title is None:
            self.app.push_screen(TraceViewerScreen(trace_groups))
        else:
            self.app.push_screen(TraceViewerScreen(trace_groups, title=title))

    async def _load_traces(self, trace_id: str | None) -> list[TraceGroup]:
        limit = None if trace_id else self._config.tui.trace_limit
        if (load_traces := self.shell.services.load_traces) is not None:
            return await load_traces(self._parquet_paths, trace_id, self._trace_id_fields, limit)
        return query_traces_from_parquet_files(
            self._parquet_paths,
            trace_id=trace_id,
            trace_id_fields=self._trace_id_fields,
            limit=limit,
        )

    @on(Input.Changed, '#search_input')
    def on_search_input_changed(self, event: Input.Changed) -> None:
        """Search as the user types, debounced by 300ms."""
        query = event.value.strip()
        self.workers.cancel_group(self, 'search')

        if not query:
            self._log_events = self._all_events
            self._load_log_events(self._all_events)
            self._update_status(f'Showing all {len(self._all_events)} events')
            return

        def execute_search() -> None:
            self.run_worker(
                self._execute_search_query(query),
                name='search',
                group='search',
                exclusive=True,
            )

        self.set_timer(0.3, execute_search)

    @on(Input.Blurred, '#search_input')
    def on_search_input_blurred(self, _event: Input.Blurred) -> None:
        """Give the rows back the three lines an empty search box was holding.

        Only the search box is watched. A handler on every descendant blur stole
        focus back from the command line the moment it opened, which left ``:``
        advertised in the footer and inert in a real terminal.
        """
        if self._search_input is not None and not self._search_input.value.strip():
            self._search_input.close()
            self.restore_focus()

    @on(Input.Submitted, '#search_input')
    def on_search_input_submitted(self, event: Input.Submitted) -> None:
        """Move focus to the table so results can be navigated."""
        event.stop()
        if self._table is None:
            return
        self._table.focus()
        if self._table.row_count > 0:
            self._table.move_cursor(row=0)

    async def _execute_search_query(self, query: str) -> None:
        try:
            self._update_status(f'Searching for: {query}...')

            filter_node = parse_query(query)

            if self._parquet_paths:
                results = list(
                    query_parquet_files_to_log_events(
                        self._parquet_paths,
                        self._session_filter(filter_node),
                        limit=self._config.tui.search_limit,
                    ),
                )
            else:
                results = self._filter_events_in_memory(query)

            self._log_events = results
            self._load_log_events(results)
            hint = _field_syntax_hint(query) if not results else ''
            self._update_status(f'Found {len(results)} matching events{hint}')

        except ValueError as err:
            self._update_status(f'Invalid query: {err}')
        except Exception as err:
            self._update_status(f'Search error: {err}')
            self.notify(f'Search failed: {err}', severity='error')

    def _session_filter(self, extra: FilterNode | None = None) -> FilterNode | None:
        """Combine the session-wide filter with a view-local one.

        The cached window holds every event in the range, so the session filter is
        applied on read rather than pushed to CloudWatch, and an in-view search
        narrows within it instead of escaping it.
        """
        pattern = self.shell.session.filter_pattern
        nodes = [node for node in (parse_filter_pattern(pattern) if pattern else None, extra) if node is not None]
        if not nodes:
            return None
        return nodes[0] if len(nodes) == 1 else combine_filters(nodes)

    def _filter_events_in_memory(self, query: str) -> list[LogEvent]:
        query_lower = query.lower()
        return [event for event in self._all_events if query_lower in event.message.lower()]
