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
from typing import ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.widgets import DataTable, Footer, Header, Label

from tail_cw.aws.client import LogEvent
from tail_cw.tui.log_viewer import batch_format_log_events
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
    ) -> None:
        """Initialize the LogTailApp.

        Args:
            log_events: Optional initial list of log events to display
            title: Application title shown in header
        """
        super().__init__()
        self.title = title
        self._log_events: list[LogEvent] = log_events if log_events is not None else []
        self._table: DataTable[str] | None = None

    def compose(self) -> ComposeResult:
        """Build the UI hierarchy.

        Yields:
            Header with clock
            Container with DataTable and status label
            Footer with keyboard shortcuts
        """
        yield Header(show_clock=True)
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
        and updates the status label.
        """
        self._table = self.query_one('#log_table', DataTable)
        self._setup_table_columns()

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

        self._table.add_columns(
            ('timestamp', 'Timestamp'),
            ('log_group', 'Log Group'),
            ('log_stream', 'Log Stream'),
            ('message', 'Message'),
            ('event_id', 'Event ID'),
        )

        # Set column widths for better display
        self._table.add_column('timestamp', width=25)
        self._table.add_column('log_group', width=30)
        self._table.add_column('log_stream', width=30)
        self._table.add_column('message', width=None)  # Flexible width
        self._table.add_column('event_id', width=20)

    def _load_log_events(self, events: list[LogEvent] | None = None) -> None:
        """Populate table with log events.

        Uses batch insertion (add_rows) for performance with large datasets.
        Clears existing data before loading new events.

        Args:
            events: Optional list of events to load. If None, uses self._log_events
        """
        if self._table is None:
            return

        events_to_load = events if events is not None else self._log_events

        # Clear existing data
        self._table.clear(columns=False)

        # Batch format and load events
        formatted_rows = batch_format_log_events(events_to_load, truncate_message=100)
        self._table.add_rows(formatted_rows)  # type: ignore[arg-type]

        # Update loading state
        self._table.loading = False

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

        cursor_coordinate = self._table.cursor_coordinate
        if cursor_coordinate.row < 0:
            # No row selected
            return

        # Get the LogEvent from our data
        try:
            log_event = self._log_events[cursor_coordinate.row]
        except IndexError:
            self.notify('Invalid row selection', severity='error')
            return

        # Push the detail modal
        self.push_screen(RecordDetailScreen(log_event))

    def action_focus_search(self) -> None:
        """Placeholder for search functionality.

        Will be implemented in the query engine phase.
        """
        self.notify('Search functionality coming in next phase', severity='information')

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

    def load_events(self, events: list[LogEvent]) -> None:
        """Public method to load new events into the table.

        This allows external code to update the displayed logs dynamically.

        Args:
            events: List of LogEvent instances to display

        Example:
            >>> app = LogTailApp()
            >>> # ... later, after fetching new logs ...
            >>> app.load_events(new_events)
        """
        self._log_events = events
        self._load_log_events(events)
        self._update_status(f'Loaded {len(events)} events')
