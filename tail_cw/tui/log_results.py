"""A screen that shows fetched log events, used when diving from a dashboard.

Reuses the query engine, the row formatting, and the record-detail modal so a
dive lands in the same log surface as ``tail-cw fetch``, filtered to the widget's
time window and log group.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any, ClassVar

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Input, Label

from tail_cw.aws.client import LogEvent
from tail_cw.config import TailCWConfig
from tail_cw.query import parse_extended_filter, parse_filter_pattern, query_parquet_file_to_log_events
from tail_cw.tui.log_viewer import batch_format_log_events, get_column_definitions
from tail_cw.tui.record_detail import RecordDetailScreen


class LogResultsScreen(Screen[None]):
    """Displays logs from a Parquet file with the same search as the main viewer."""

    CSS = """
    LogResultsScreen { layout: vertical; }
    #results_search { dock: top; height: 3; padding: 0 1; border: solid $accent; }
    #results_table { height: 1fr; width: 100%; }
    #results_status { height: 1; width: 100%; background: $panel; color: $text; padding: 0 1; }
    """

    BINDINGS: ClassVar[Sequence[Binding]] = [  # type: ignore[assignment]
        Binding('escape', 'dismiss', 'Back', show=True),
        Binding('/', 'focus_search', 'Search', show=True),
        Binding('enter', 'show_detail', 'Detail', show=True),
    ]

    def __init__(self, parquet_path: Path, config: TailCWConfig, *, title: str) -> None:
        """Show logs from the given Parquet file under the given title."""
        super().__init__()
        self._parquet_path = parquet_path
        self._config = config
        self._title = title
        self._events: list[LogEvent] = []
        self._table: DataTable[Any] | None = None

    def compose(self) -> ComposeResult:
        """Build the search input, results table, status line, and footer.

        Yields:
            The search input, a container with the table and status label, and the footer.
        """
        yield Input(placeholder='Search (CloudWatch syntax or key:value)...', id='results_search')
        with Container():
            yield DataTable(id='results_table', show_header=True, cursor_type='row', zebra_stripes=True)
            yield Label(self._title, id='results_status')
        yield Footer()

    def on_mount(self) -> None:
        """Set up columns and load the initial events."""
        self._table = self.query_one('#results_table', DataTable)
        for key, label in get_column_definitions():
            self._table.add_column(label, key=key)
        self._load(None)
        self._table.focus()

    def _load(self, query: str | None) -> None:
        if self._table is None:
            return
        filter_node = self._parse_query(query) if query else None
        limit = self._config.tui.initial_load_limit if query is None else self._config.tui.search_limit
        try:
            self._events = list(query_parquet_file_to_log_events(self._parquet_path, filter_node, limit=limit))
        except (ValueError, FileNotFoundError) as err:
            self.query_one('#results_status', Label).update(f'Query error: {err}')
            return
        self._table.clear(columns=False)
        self._table.add_rows(batch_format_log_events(self._events, truncate_message=100))
        self.query_one('#results_status', Label).update(f'{self._title} · {len(self._events)} events')

    @staticmethod
    def _parse_query(query: str) -> Any:
        text = query.strip()
        if ':' in text and not text.startswith(('{', '"', '%')):
            return parse_extended_filter(text)
        return parse_filter_pattern(text)

    def action_focus_search(self) -> None:
        """Focus the search input."""
        self.query_one('#results_search', Input).focus()

    def action_show_detail(self) -> None:
        """Open the record-detail modal for the selected row."""
        if self._table is None or self._table.cursor_row < 0:
            return
        try:
            event = self._events[self._table.cursor_row]
        except IndexError:
            return
        self.app.push_screen(RecordDetailScreen(event))

    @on(Input.Submitted, '#results_search')
    def _on_submit(self, event: Input.Submitted) -> None:
        event.stop()
        self._load(event.value)
        if self._table is not None:
            self._table.focus()
