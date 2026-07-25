"""The log view: a historical window and a live stream behind one toggle.

One screen owns both halves on purpose. Flipping ``L`` swaps the data source
while the group selection, filter, and window stay on the shared session, so a
search you like becomes a tail without retyping it. Every AWS touch arrives as
a callable on ``ShellServices``; the screen itself reads Parquet and renders.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any, ClassVar

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.message import Message
from textual.timer import Timer
from textual.widgets import DataTable, Input, Label
from textual.worker import get_current_worker

from tail_cw.aws.client import LogEvent
from tail_cw.config import TailCWConfig
from tail_cw.query import parse_extended_filter, parse_filter_pattern, query_parquet_files_to_log_events
from tail_cw.query.trace import extract_trace_id_from_event, query_traces_from_parquet
from tail_cw.tui.log_viewer import batch_format_log_events, get_column_definitions
from tail_cw.tui.navigation import NavTarget, ViewKind
from tail_cw.tui.record_detail import RecordDetailScreen
from tail_cw.tui.shell import ShellCommand, ShellScreen
from tail_cw.tui.trace_viewer import TraceViewerScreen

LiveStreamFactory = Callable[[], Iterator[LogEvent]]

_LIVE_FLUSH_INTERVAL_SECONDS = 0.25
_MESSAGE_TRUNCATE = 100


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

    #search_input {
        dock: top;
        height: 3;
        padding: 0 1;
        border: solid $accent;
    }

    #log_table {
        height: 1fr;
        width: 100%;
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
    ]

    def __init__(self, log_groups: Sequence[str], *, live: bool = False) -> None:
        """Open the view over the given groups, streaming when ``live`` is set."""
        super().__init__()
        self._log_groups: list[str] = list(log_groups)
        self._live_mode = live
        self._log_events: list[LogEvent] = []
        self._all_events: list[LogEvent] = []
        self._table: DataTable[Any] | None = None
        self._search_input: Input | None = None
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
        yield Input(
            placeholder='Search (CloudWatch syntax or key:value)...',
            id='search_input',
        )
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
        self._search_input = self.query_one('#search_input', Input)
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
            'trace': ShellCommand('Open the trace view over the loaded events'),
        }

    def run_view_command(self, name: str, argument: str) -> bool:
        """Run ``:live`` or ``:trace``, leaving anything else to the shell."""
        del argument
        match name:
            case 'live':
                self.action_toggle_live()
            case 'trace':
                self.action_toggle_trace_view()
            case _:
                return False
        return True

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
        if self._table is None:
            return

        column_widths = {
            'timestamp': 25,
            'log_group': 30,
            'log_stream': 30,
            'message': None,
            'event_id': 20,
        }
        for key, label in get_column_definitions():
            self._table.add_column(label, key=key, width=column_widths.get(key))

    def _load_window(self) -> None:
        resolve = self.shell.services.resolve_logs
        if resolve is None:
            self._update_status('No log source available')
            return
        self._update_status('Loading events...')
        session = self.shell.session
        self.run_worker(
            partial(
                self._resolve_in_thread,
                resolve,
                session.start,
                session.end,
                session.filter_pattern,
            ),
            name='resolve_logs',
            group='resolve_logs',
            exclusive=True,
            thread=True,
        )

    def _resolve_in_thread(
        self,
        resolve: Callable[[Sequence[str], datetime, datetime, str | None], list[Path]],
        start: datetime,
        end: datetime,
        filter_pattern: str | None,
    ) -> None:
        try:
            paths = resolve(tuple(self._log_groups), start, end, filter_pattern)
        except Exception as err:
            self.app.call_from_thread(self.notify, f'Failed to load logs: {err}', severity='error')
            self.app.call_from_thread(self._update_status, f'Load error: {err}')
            return
        self.app.call_from_thread(self.set_parquet_sources, paths)

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

        if self._search_input is not None:
            self._search_input.display = True

        initial_limit = self._config.tui.initial_load_limit
        try:
            events = list(query_parquet_files_to_log_events(self._parquet_paths, None, limit=initial_limit))
        except Exception as err:
            self.notify(f'Failed to load Parquet file: {err}', severity='error')
            self._update_status(f'Error loading Parquet: {err}')
            return

        self._log_events = events
        self._all_events = events.copy()
        self._load_log_events(events)
        self._update_status(f'Loaded {len(events)} events (showing first {min(len(events), initial_limit)})')

    def load_events(self, events: list[LogEvent], parquet_paths: Sequence[Path] | None = None) -> None:
        """Replace the displayed events, optionally pointing search at new files."""
        self._log_events = events
        self._all_events = events.copy()
        self._load_log_events(events)

        if parquet_paths is not None:
            self._parquet_paths = list(parquet_paths)

        if self._search_input is not None and (self._parquet_paths or self._all_events):
            self._search_input.display = True

        self._update_status(f'Loaded {len(events)} events')

    def _load_log_events(self, events: list[LogEvent] | None = None) -> None:
        if self._table is None:
            return

        events_to_load = events if events is not None else self._log_events
        self._table.clear(columns=False)
        self._table.loading = True

        if len(events_to_load) > self._config.tui.chunk_threshold:
            self.run_worker(
                self._load_events_incrementally(events_to_load, self._config.tui.chunk_size),
                name='load_events',
                exclusive=True,
            )
        else:
            self._table.add_rows(batch_format_log_events(events_to_load, truncate_message=_MESSAGE_TRUNCATE))
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
                self._table.add_rows(batch_format_log_events(chunk, truncate_message=_MESSAGE_TRUNCATE))
            self._post_progress(end_idx, total, 'Loading events')

        if self._table is not None:
            self._table.loading = False
        self._update_status(f'Loaded {total} events')

    def _post_progress(self, current: int, total: int, status: str) -> None:
        self.post_message(ProgressUpdate(current=current, total=total, status=status))

    def _update_status(self, message: str) -> None:
        self.query_one('#status', Label).update(message)

    def on_progress_update(self, message: ProgressUpdate) -> None:
        """Show a worker's progress on the status line."""
        if message.total > 0:
            status_text = f'{message.status} ({message.current}/{message.total})'
        else:
            status_text = f'{message.status} ({message.current} events)'
        self._update_status(status_text)
        message.stop()

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
        self._search_input.focus()

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
        self._log_events = []
        self._all_events = []
        self._load_log_events([])
        self.start_live_tail(partial(stream, tuple(self._log_groups), self.shell.session.filter_pattern))

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

        The factory is invoked once, in a background thread. Events are
        coalesced and rendered in batches; call this after the screen mounts.
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
            self._consume_live_stream,
            name='live_tail',
            group='live_tail',
            exclusive=True,
            thread=True,
        )

    def _consume_live_stream(self) -> None:
        if self._live_stream_factory is None:
            return
        worker = get_current_worker()
        try:
            for event in self._live_stream_factory():
                if worker.is_cancelled:
                    return
                self._pending_live_events.append(event)
        except Exception as err:
            self.app.call_from_thread(self._finish_live_tail, f'Live tail stopped: {err}')
            return
        self.app.call_from_thread(self._finish_live_tail, 'Live tail stream ended')

    def _finish_live_tail(self, message: str) -> None:
        self._flush_live_events()
        self._live_active = False
        self.notify(message, severity='warning')
        self._update_live_status()

    def note_live_sampled(self, sampled: bool) -> None:  # ruff: ignore[boolean-type-hint-positional-argument]
        """Record whether the server is sampling the live stream (thread-safe)."""
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
            self._table.add_rows(batch_format_log_events(drained, truncate_message=_MESSAGE_TRUNCATE))

    def _rebuild_live_table(self) -> None:
        if self._table is None:
            return
        self._log_events = list(self._live_buffer)
        self._table.clear(columns=False)
        self._table.add_rows(batch_format_log_events(self._log_events, truncate_message=_MESSAGE_TRUNCATE))

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
        """Open the trace view over every trace in the loaded data.

        Trace queries read one file, so a multi-group view traces its first
        group only.
        """
        if not self._parquet_paths:
            self.notify('Trace view requires Parquet data source', severity='warning')
            return

        try:
            self._update_status('Loading traces...')
            trace_groups = query_traces_from_parquet(
                self._parquet_paths[0],
                trace_id_fields=self._trace_id_fields,
                limit=self._config.tui.trace_limit,
            )
        except Exception as err:
            self.notify(f'Failed to load traces: {err}', severity='error')
            self._update_status(f'Trace loading error: {err}')
            return

        if not trace_groups:
            self.notify('No traces found in current data', severity='information')
            self._update_status('No traces found')
            return

        self.app.push_screen(TraceViewerScreen(trace_groups))

    def action_show_trace_for_selected(self) -> None:
        """Open the trace view for the selected event's trace.

        Trace queries read one file, so a multi-group view traces its first
        group only.
        """
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

        try:
            self._update_status(f'Loading trace {trace_id[:8]}...')
            trace_groups = query_traces_from_parquet(
                self._parquet_paths[0],
                trace_id=trace_id,
                trace_id_fields=self._trace_id_fields,
            )
        except Exception as err:
            self.notify(f'Failed to load trace: {err}', severity='error')
            self._update_status(f'Trace loading error: {err}')
            return

        if not trace_groups:
            self.notify(f'Trace not found in current data: {trace_id}', severity='information')
            self._update_status('Trace not found')
            return

        self.app.push_screen(TraceViewerScreen(trace_groups, title=f'Trace: {trace_id[:16]}...'))

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

            if query.startswith(('{', '"')) or (query.startswith('%') and query.endswith('%')):
                filter_node = parse_filter_pattern(query)
            elif ':' in query:
                filter_node = parse_extended_filter(query)
            else:
                filter_node = parse_filter_pattern(query)

            if self._parquet_paths:
                results = list(
                    query_parquet_files_to_log_events(
                        self._parquet_paths,
                        filter_node,
                        limit=self._config.tui.search_limit,
                    ),
                )
            else:
                results = self._filter_events_in_memory(query)

            self._log_events = results
            self._load_log_events(results)
            self._update_status(f'Found {len(results)} matching events')

        except ValueError as err:
            self._update_status(f'Invalid query: {err}')
        except Exception as err:
            self._update_status(f'Search error: {err}')
            self.notify(f'Search failed: {err}', severity='error')

    def _filter_events_in_memory(self, query: str) -> list[LogEvent]:
        query_lower = query.lower()
        return [event for event in self._all_events if query_lower in event.message.lower()]
