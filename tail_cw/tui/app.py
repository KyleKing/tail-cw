"""Main Textual application for CloudWatch Logs TUI viewer.

This module provides the LogTailApp class, a Textual-based terminal user interface
for viewing and interacting with AWS CloudWatch log events. The app displays logs
in a tabular format with support for detailed record inspection, keyboard navigation,
and future search capabilities.

The app follows the functions-over-classes approach where possible, with pure
formatting logic extracted to separate modules (log_viewer.py).
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any, ClassVar

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.widgets import DataTable, Footer, Header, Input, Label
from textual.worker import get_current_worker

from tail_cw.aws.client import LogEvent
from tail_cw.query import parse_extended_filter, parse_filter_pattern, query_parquet_file_to_log_events
from tail_cw.tui.log_viewer import batch_format_log_events, get_column_definitions
from tail_cw.tui.record_detail import RecordDetailScreen


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

    Keyboard shortcuts:
        - q: Quit the application
        - /: Focus search (placeholder for future phase)
        - Enter: Show detail modal for selected row
        - r: Refresh data (placeholder)
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
        Binding('?', 'help', 'Help', show=False),
    ]

    def __init__(
        self,
        log_events: list[LogEvent] | None = None,
        title: str = 'CloudWatch Logs Viewer',
        parquet_path: Path | None = None,
    ) -> None:
        """Initialize the LogTailApp.

        Args:
            log_events: Optional initial list of log events to display
            title: Application title shown in header
            parquet_path: Optional path to Parquet file for search functionality
        """
        super().__init__()
        self.title = title
        self._log_events: list[LogEvent] = log_events if log_events is not None else []
        self._all_events: list[LogEvent] = []  # Full unfiltered dataset for fallback
        self._table: DataTable[Any] | None = None
        self._search_input: Input | None = None
        self._parquet_path: Path | None = parquet_path
        self._search_debounce_timer: str | None = None  # For debouncing search

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
        if self._parquet_path is None and not self._all_events:
            self._search_input.display = False

        if self._log_events:
            self._load_log_events()
            self._update_status(f'Loaded {len(self._log_events)} events')
        else:
            self._update_status('No logs loaded')

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
        chunk_threshold = 5000
        chunk_size = 1000

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
            progress = end_idx / total * 100
            self._update_status(f'Loading events: {end_idx}/{total} ({progress:.0f}%)')

        # Finalize loading
        if self._table is not None:
            self._table.loading = False
        self._update_status(f'Loaded {total} events')

    def _update_status(self, message: str) -> None:
        """Update the status label with a message.

        Args:
            message: Status message to display
        """
        status_label = self.query_one('#status', Label)
        status_label.update(message)

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

    def action_refresh(self) -> None:
        """Placeholder for refresh functionality.

        Could trigger re-fetching from cache or AWS in future phases.
        """
        self.notify('Refresh functionality coming soon', severity='information')

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
                results = list(
                    query_parquet_file_to_log_events(
                        self._parquet_path,
                        filter_node,
                        limit=10000,  # Reasonable limit for UI
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
            events = list(
                query_parquet_file_to_log_events(
                    parquet_path,
                    None,  # No filter - load all
                    limit=1000,  # Initial load limit
                ),
            )

            self._log_events = events
            self._all_events = events.copy()
            self._load_log_events(events)
            self._update_status(f'Loaded {len(events)} events from Parquet (showing first 1000)')

        except Exception as e:
            self.notify(f'Failed to load Parquet file: {e}', severity='error')
            self._update_status(f'Error loading Parquet: {e}')
