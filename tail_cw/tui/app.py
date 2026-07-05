"""Main Textual application for CloudWatch Logs TUI viewer.

This module provides the LogTailApp class, a Textual-based terminal user interface
for viewing and interacting with AWS CloudWatch log events. The app displays logs
in a tabular format with support for detailed record inspection, keyboard navigation,
and future search capabilities.

The app follows the functions-over-classes approach where possible, with pure
formatting logic extracted to separate modules (log_viewer.py).
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any, ClassVar

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.message import Message
from textual.widgets import DataTable, Footer, Header, Input, Label
from textual.worker import get_current_worker

from tail_cw.aws.client import LogEvent
from tail_cw.cli import FetchRequest
from tail_cw.config import TailCWConfig, load_config
from tail_cw.query import parse_extended_filter, parse_filter_pattern, query_parquet_file_to_log_events
from tail_cw.query.trace import extract_trace_id_from_event, query_traces_from_parquet
from tail_cw.tui.log_viewer import batch_format_log_events, get_column_definitions
from tail_cw.tui.record_detail import RecordDetailScreen
from tail_cw.tui.trace_viewer import TraceViewerScreen

LiveStreamFactory = Callable[[], Iterator[LogEvent]]

_LIVE_FLUSH_INTERVAL_SECONDS = 0.25


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


class LogTailApp(App[None]):
    """Textual application for viewing CloudWatch log events.

    This app provides a terminal-based UI for browsing log events in a table format.
    Users can navigate with arrow keys, view detailed records with Enter, and use
    various keyboard shortcuts for common operations.

    Features:
        - Tabular display of log events with formatted timestamps
        - Keyboard navigation (arrows, Enter, q, etc.)
        - Detail modal for full record inspection
        - Batch loading for performance with large datasets
        - Status bar showing event counts
        - Extensible design for future search/filter capabilities
        - Config-driven pagination, search limits, and trace discovery

    Keyboard shortcuts:
        - q: Quit the application
        - /: Focus search (placeholder for future phase)
        - Enter: Show detail modal for selected row
        - r: Refresh data (re-fetch the current range extended to now)
        - t: Toggle trace view (shows all traces)
        - T: Show trace for selected log event
        - ?: Show help

    Args:
        log_events: Optional initial list of log events to display
        title: Application title shown in header (default: 'CloudWatch Logs Viewer')

    Example:
        >>> from tail_cw.tui.app import LogTailApp
        >>> events = [...]  # List of LogEvent instances
        >>> app = LogTailApp(log_events=events)
        >>> app.run()
    """

    CSS = """
    Screen {
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

    BINDINGS: ClassVar[Sequence[Binding]] = [  # type: ignore[assignment]
        Binding('q', 'quit', 'Quit', show=True),
        Binding('/', 'focus_search', 'Search', show=True),
        Binding('enter', 'show_detail', 'Detail', show=True),
        Binding('r', 'refresh', 'Refresh', show=True),
        Binding('space', 'toggle_live_pause', 'Pause/Resume', show=False),
        Binding('t', 'toggle_trace_view', 'Trace View', show=True),
        Binding('shift+t', 'show_trace_for_selected', 'Show Trace', show=True),
        Binding('?', 'help', 'Help', show=False),
    ]

    def __init__(
        self,
        log_events: list[LogEvent] | None = None,
        title: str = 'CloudWatch Logs Viewer',
        parquet_path: Path | None = None,
        trace_id_fields: list[str] | None = None,
        config: TailCWConfig | None = None,
    ) -> None:
        """Initialize the LogTailApp.

        Args:
            log_events: Optional initial list of log events to display
            title: Application title shown in header
            parquet_path: Optional path to Parquet file for search functionality
            trace_id_fields: Optional list of field names to search for trace IDs.
                Defaults to values defined in the configuration when omitted.
            config: Optional application configuration. When not supplied the
                configuration is loaded from the default location.
        """
        super().__init__()
        self.title = title
        self._config = config if config is not None else load_config()
        self._log_events: list[LogEvent] = log_events if log_events is not None else []
        self._all_events: list[LogEvent] = []  # Full unfiltered dataset for fallback
        self._table: DataTable[Any] | None = None
        self._search_input: Input | None = None
        self._parquet_path: Path | None = parquet_path
        self._search_debounce_timer: str | None = None  # For debouncing search
        self._fetch_request: FetchRequest | None = None
        self._refetch: Callable[[FetchRequest], Path | None] | None = None
        trace_fields = trace_id_fields if trace_id_fields is not None else list(self._config.trace.trace_id_fields)
        self._trace_id_fields: list[str] = trace_fields
        self._live_stream_factory: LiveStreamFactory | None = None
        self._live_buffer: deque[LogEvent] = deque(maxlen=self._config.tui.live_buffer_limit)
        self._pending_live_events: deque[LogEvent] = deque()
        self._live_active = False
        self._live_paused = False
        self._live_sampled = False
        self._live_event_count = 0

    def compose(self) -> ComposeResult:  # noqa: PLR6301
        """Build the UI hierarchy.

        Yields:
            Header with clock
            Search input (at top)
            Container with DataTable and status label
            Footer with keyboard shortcuts
        """
        yield Header(show_clock=True)
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
        yield Footer()

    def on_mount(self) -> None:
        """Called after widgets are mounted.

        Sets up the DataTable columns, loads initial events if provided,
        configures search input, and updates the status label.
        """
        self._table = self.query_one('#log_table', DataTable)
        self._search_input = self.query_one('#search_input', Input)
        self._setup_table_columns()

        # Store full dataset for filtering
        self._all_events = self._log_events.copy() if self._log_events else []

        # Hide search input if no data source
        if self._parquet_path is None and not self._all_events and self._live_stream_factory is None:
            self._search_input.display = False

        if self._log_events:
            self._load_log_events()
            self._update_status(f'Loaded {len(self._log_events)} events')
        elif self._parquet_path is not None:
            self.set_parquet_source(self._parquet_path)
        elif self._live_stream_factory is None:
            self._update_status('No logs loaded')

        if self._live_stream_factory is not None:
            self._begin_live_tail()

        if self._table is not None:
            self._table.focus()

    def _setup_table_columns(self) -> None:
        """Configure DataTable columns.

        Sets up 5 columns: Timestamp, Log Group, Log Stream, Message, Event ID.
        Column widths are configured for optimal display.
        """
        if self._table is None:
            return

        # Width policy: fixed for most columns, flexible for message
        column_widths = {
            'timestamp': 25,
            'log_group': 30,
            'log_stream': 30,
            'message': None,  # Flexible width
            'event_id': 20,
        }

        # Add columns using centralized definitions
        for key, label in get_column_definitions():
            width = column_widths.get(key)
            self._table.add_column(label, key=key, width=width)

    def _load_log_events(self, events: list[LogEvent] | None = None) -> None:
        """Populate table with log events.

        Uses batch insertion (add_rows) for performance with large datasets.
        For very large datasets (>5,000 events), uses incremental loading
        to keep the UI responsive.

        Args:
            events: Optional list of events to load. If None, uses self._log_events
        """
        if self._table is None:
            return

        events_to_load = events if events is not None else self._log_events

        # Clear existing data
        self._table.clear(columns=False)

        # Set loading state
        self._table.loading = True

        # Threshold for incremental loading
        chunk_threshold = self._config.tui.chunk_threshold
        chunk_size = self._config.tui.chunk_size

        if len(events_to_load) > chunk_threshold:
            # Use incremental loading for large datasets
            self.run_worker(
                self._load_events_incrementally(events_to_load, chunk_size),
                name='load_events',
                exclusive=True,
            )
        else:
            # Direct load for smaller datasets
            formatted_rows = batch_format_log_events(events_to_load, truncate_message=100)
            self._table.add_rows(formatted_rows)
            self._table.loading = False

    async def _load_events_incrementally(
        self,
        events: list[LogEvent],
        chunk_size: int,
    ) -> None:
        """Load events in chunks to keep UI responsive.

        Args:
            events: List of events to load
            chunk_size: Number of events per chunk
        """
        worker = get_current_worker()
        total = len(events)

        for start_idx in range(0, total, chunk_size):
            # Check if worker was cancelled
            if worker.is_cancelled:
                break

            end_idx = min(start_idx + chunk_size, total)
            chunk = events[start_idx:end_idx]

            # Format and add chunk
            formatted_rows = batch_format_log_events(chunk, truncate_message=100)

            if self._table is not None:
                self._table.add_rows(formatted_rows)

            # Update progress
            self._post_progress(end_idx, total, 'Loading events')

        # Finalize loading
        if self._table is not None:
            self._table.loading = False
        self._update_status(f'Loaded {total} events')

    def _post_progress(self, current: int, total: int, status: str) -> None:
        """Post a progress update message to the app."""
        self.post_message(ProgressUpdate(current=current, total=total, status=status))

    def _update_status(self, message: str) -> None:
        """Update the status label with a message.

        Args:
            message: Status message to display
        """
        status_label = self.query_one('#status', Label)
        status_label.update(message)

    def on_progress_update(self, message: ProgressUpdate) -> None:
        """Handle progress updates from background workers."""
        if message.total > 0:
            status_text = f'{message.status} ({message.current}/{message.total})'
        else:
            status_text = f'{message.status} ({message.current} events)'
        self._update_status(status_text)
        message.stop()

    def action_show_detail(self) -> None:
        """Handle Enter key to show detail modal for selected row.

        Gets the selected row from the DataTable cursor, retrieves the
        corresponding LogEvent, and pushes a RecordDetailScreen modal.
        """
        if self._table is None:
            return

        row_index = self._table.cursor_row
        if row_index < 0 or not self._log_events:
            # No row selected or no events
            return

        # Get the LogEvent from our data
        try:
            log_event = self._log_events[row_index]
        except IndexError:
            self.notify('Invalid row selection', severity='error')
            return

        # Push the detail modal
        self.push_screen(RecordDetailScreen(log_event))

    def action_focus_search(self) -> None:
        """Focus the search input widget.

        Activates search mode by focusing the search input. If no data source
        is available (no parquet file and no events), shows a notification.
        """
        if self._search_input is None:
            return

        if self._parquet_path is None and not self._all_events:
            self.notify('No data source available for search', severity='warning')
            return

        self._search_input.focus()

    def set_fetch_context(
        self,
        request: FetchRequest,
        refetch: Callable[[FetchRequest], Path | None],
    ) -> None:
        """Store the fetch parameters and re-fetch callable used by refresh.

        Args:
            request: Parameters of the fetch that produced the current data.
            refetch: Callable that resolves a request to a Parquet path,
                returning None when no events match.
        """
        self._fetch_request = request
        self._refetch = refetch

    def action_refresh(self) -> None:
        """Re-fetch the current log group with the end time extended to now.

        Requires a fetch context (set via set_fetch_context when launched from
        the CLI); otherwise shows a notification.
        """
        if self._fetch_request is None or self._refetch is None:
            self.notify('Refresh requires a fetch context (launch via tail-cw fetch)', severity='information')
            return

        updated = replace(self._fetch_request, end_time=datetime.now(tz=UTC))
        self._update_status('Refreshing...')
        self.run_worker(
            partial(self._refresh_from_source, updated),
            name='refresh',
            exclusive=True,
            thread=True,
        )

    def _refresh_from_source(self, request: FetchRequest) -> None:
        if self._refetch is None:
            return
        try:
            parquet_path = self._refetch(request)
        except Exception as err:
            self.call_from_thread(self.notify, f'Refresh failed: {err}', severity='error')
            self.call_from_thread(self._update_status, f'Refresh error: {err}')
            return

        if parquet_path is None:
            self.call_from_thread(self.notify, 'No events found for the refreshed range', severity='warning')
            self.call_from_thread(self._update_status, 'Refresh found no events')
            return

        self._fetch_request = request
        self.call_from_thread(self.set_parquet_source, parquet_path)

    def start_live_tail(self, stream_factory: LiveStreamFactory) -> None:
        """Enable live streaming mode fed by the given stream factory.

        The factory is invoked once, in a background thread, after the app is
        mounted. Events are coalesced into a bounded ring buffer and rendered
        in batches; call this before ``run()`` or while the app is running.
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
        self.set_interval(_LIVE_FLUSH_INTERVAL_SECONDS, self._flush_live_events)
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
            self.call_from_thread(self._finish_live_tail, f'Live tail stopped: {err}')
            return
        self.call_from_thread(self._finish_live_tail, 'Live tail stream ended')

    def _finish_live_tail(self, message: str) -> None:
        self._flush_live_events()
        self._live_active = False
        self.notify(message, severity='warning')
        self._update_live_status()

    def note_live_sampled(self, sampled: bool) -> None:  # noqa: FBT001
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
            self._table.add_rows(batch_format_log_events(drained, truncate_message=100))

    def _rebuild_live_table(self) -> None:
        if self._table is None:
            return
        self._log_events = list(self._live_buffer)
        self._table.clear(columns=False)
        self._table.add_rows(batch_format_log_events(self._log_events, truncate_message=100))

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
        """Toggle between log table and trace view.

        Queries all traces from the Parquet data source and displays them
        in a hierarchical tree view. Requires a Parquet data source.
        """
        if self._parquet_path is None:
            self.notify(
                'Trace view requires Parquet data source',
                severity='warning',
            )
            return

        try:
            # Query for all traces with a reasonable limit to avoid UI stalls
            self._update_status('Loading traces...')
            trace_limit = self._config.tui.trace_limit
            trace_groups = query_traces_from_parquet(
                self._parquet_path,
                trace_id_fields=self._trace_id_fields,
                limit=trace_limit,
            )

            if not trace_groups:
                self.notify(
                    'No traces found in current data',
                    severity='information',
                )
                self._update_status('No traces found')
                return

            # Push trace viewer screen
            self.push_screen(TraceViewerScreen(trace_groups))

        except Exception as e:
            self.notify(
                f'Failed to load traces: {e}',
                severity='error',
            )
            self._update_status(f'Trace loading error: {e}')

    def action_show_trace_for_selected(self) -> None:
        """Show trace for currently selected log event.

        Extracts the trace ID from the selected event and displays
        all events in that trace.
        """
        if self._parquet_path is None:
            self.notify(
                'Trace view requires Parquet data source',
                severity='warning',
            )
            return

        if self._table is None:
            return

        row_index = self._table.cursor_row
        if row_index < 0 or not self._log_events:
            self.notify('No row selected', severity='warning')
            return

        # Get the selected LogEvent
        try:
            log_event = self._log_events[row_index]
        except IndexError:
            self.notify('Invalid row selection', severity='error')
            return

        # Extract trace ID
        trace_id = extract_trace_id_from_event(log_event, self._trace_id_fields)
        if not trace_id:
            self.notify(
                'No trace ID found in selected event',
                severity='information',
            )
            return

        try:
            # Query for specific trace
            self._update_status(f'Loading trace {trace_id[:8]}...')
            trace_groups = query_traces_from_parquet(
                self._parquet_path,
                trace_id=trace_id,
                trace_id_fields=self._trace_id_fields,
            )

            if not trace_groups:
                self.notify(
                    f'Trace not found in current data: {trace_id}',
                    severity='information',
                )
                self._update_status('Trace not found')
                return

            # Push trace viewer screen with single trace
            self.push_screen(TraceViewerScreen(trace_groups, title=f'Trace: {trace_id[:16]}...'))

        except Exception as e:
            self.notify(
                f'Failed to load trace: {e}',
                severity='error',
            )
            self._update_status(f'Trace loading error: {e}')

    def action_help(self) -> None:
        """Show help information about keyboard shortcuts.

        Displays all visible bindings in a notification.
        """
        help_text = 'Keyboard Shortcuts:\n\n'
        for binding in self.BINDINGS:
            if binding.show:
                help_text += f'  {binding.key}: {binding.description}\n'

        self.notify(help_text, title='Help', severity='information', timeout=10)

    @on(Input.Changed, '#search_input')
    def on_search_input_changed(self, event: Input.Changed) -> None:
        """Handle live search as user types.

        Uses debouncing (300ms) to avoid excessive queries while typing.
        The actual search is performed in a worker to keep the UI responsive.

        Args:
            event: Input changed event containing the new query value
        """
        query = event.value.strip()

        # Cancel any existing search worker
        self.workers.cancel_group(self, 'search')

        # Empty query - restore all events immediately
        if not query:
            self._log_events = self._all_events
            self._load_log_events(self._all_events)
            self._update_status(f'Showing all {len(self._all_events)} events')
            return

        # Set up debounce timer (300ms)
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
        """Handle Enter key in search input.

        Moves focus to the DataTable to allow navigation of search results.

        Args:
            event: Input submitted event
        """
        event.stop()

        if self._table is None:
            return

        # Focus table for navigation
        self._table.focus()

        # Move cursor to first row if available
        if self._table.row_count > 0:
            self._table.move_cursor(row=0)

    async def _execute_search_query(self, query: str) -> None:
        """Execute search query in background worker.

        Parses the query and filters events, then updates the UI with results.
        This method runs asynchronously to keep the UI responsive.

        Args:
            query: Search query string
        """
        try:
            # Update status to show searching
            self._update_status(f'Searching for: {query}...')

            # Try CloudWatch syntax first
            filter_node = None
            if query.startswith(('{', '"')) or (query.startswith('%') and query.endswith('%')):
                filter_node = parse_filter_pattern(query)
            elif ':' in query:
                # Try extended syntax
                filter_node = parse_extended_filter(query)
            else:
                # Default to text search
                filter_node = parse_filter_pattern(query)

            # Execute search
            if self._parquet_path is not None:
                # Query Parquet file
                search_limit = self._config.tui.search_limit
                results = list(
                    query_parquet_file_to_log_events(
                        self._parquet_path,
                        filter_node,
                        limit=search_limit,  # Configured limit for UI
                    ),
                )
            else:
                # Filter in-memory events with simple text search
                results = self._filter_events_in_memory(query)

            # Update backing data and table with results
            self._log_events = results
            self._load_log_events(results)
            self._update_status(f'Found {len(results)} matching events')

        except ValueError as e:
            # Invalid query - show error in status
            self._update_status(f'Invalid query: {e}')
        except Exception as e:
            # Other errors
            self._update_status(f'Search error: {e}')
            self.notify(f'Search failed: {e}', severity='error')

    def _filter_events_in_memory(self, query: str) -> list[LogEvent]:
        """Filter events in memory with simple text search.

        Used as fallback when no Parquet file is available. Performs
        case-insensitive text search in message field.

        Args:
            query: Search query string

        Returns:
            List of matching LogEvent instances
        """
        query_lower = query.lower()
        return [event for event in self._all_events if query_lower in event.message.lower()]

    def load_events(
        self,
        events: list[LogEvent],
        parquet_path: Path | None = None,
    ) -> None:
        """Public method to load new events into the table.

        This allows external code to update the displayed logs dynamically.

        Args:
            events: List of LogEvent instances to display
            parquet_path: Optional path to Parquet file for search functionality

        Example:
            >>> app = LogTailApp()
            >>> # ... later, after fetching new logs ...
            >>> app.load_events(new_events)
        """
        self._log_events = events
        self._all_events = events.copy()
        self._load_log_events(events)

        # Update parquet path if provided
        if parquet_path is not None:
            self._parquet_path = parquet_path

        # Show search input if we now have data
        if self._search_input is not None and (self._parquet_path or self._all_events):
            self._search_input.display = True

        self._update_status(f'Loaded {len(events)} events')

    def set_parquet_source(self, parquet_path: Path) -> None:
        """Set Parquet file as data source for querying.

        Loads initial events from the Parquet file and enables search functionality.

        Args:
            parquet_path: Path to Parquet file containing log events

        Raises:
            FileNotFoundError: If Parquet file doesn't exist

        Example:
            >>> app = LogTailApp()
            >>> app.set_parquet_source(Path('logs.parquet'))
        """
        if not parquet_path.exists():
            msg = f'Parquet file not found: {parquet_path}'
            raise FileNotFoundError(msg)

        self._parquet_path = parquet_path

        # Show search input
        if self._search_input is not None:
            self._search_input.display = True

        # Load initial data (limited for performance)
        try:
            initial_limit = self._config.tui.initial_load_limit
            events = list(
                query_parquet_file_to_log_events(
                    parquet_path,
                    None,  # No filter - load all
                    limit=initial_limit,  # Initial load limit
                ),
            )

            self._log_events = events
            self._all_events = events.copy()
            self._load_log_events(events)
            self._update_status(
                f'Loaded {len(events)} events from Parquet (showing first {min(len(events), initial_limit)})',
            )

        except Exception as e:
            self.notify(f'Failed to load Parquet file: {e}', severity='error')
            self._update_status(f'Error loading Parquet: {e}')
