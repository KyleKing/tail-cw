"""Shared list-plus-detail scaffolding for the browser views.

The group browser and the dashboard browser are the same shape: a filterable
table on the left, a pane on the right describing whatever row the cursor sits
on. That pane costs an API call per row, so the debounce that guards it lives
here too rather than being written twice.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import DataTable, Input, Label, Static

from tail_cw.aws.log_groups import LogGroupInfo, resolve_group_pattern

DEBOUNCE_SECONDS = 0.25
"""Delay before a highlighted row triggers its detail fetch."""

SELECTED_MARKER = '●'
"""Glyph marking a multi-selected row."""

_BYTE_UNITS = ('B', 'KB', 'MB', 'GB', 'TB', 'PB')
_BYTE_STEP = 1024.0
_SECONDS_PER_MINUTE = 60
_SECONDS_PER_HOUR = 3600


@dataclass(frozen=True)
class PickerColumn:
    """One table column: its cell key, header label, and optional fixed width."""

    key: str
    label: str
    width: int | None = None


def humanize_bytes(value: int | None) -> str:
    """Render a byte count in the largest unit that keeps it under 1024."""
    if value is None:
        return '-'
    size = float(value)
    for unit in _BYTE_UNITS:
        if size < _BYTE_STEP or unit == _BYTE_UNITS[-1]:
            return f'{size:.0f} {unit}' if unit == 'B' else f'{size:.1f} {unit}'
        size /= _BYTE_STEP
    raise AssertionError  # pragma: no cover - the loop always returns


def format_retention(days: int | None) -> str:
    """Render a retention setting, naming the never-expires case."""
    return 'never' if days is None else f'{days}d'


def format_created(created: datetime | None) -> str:
    """Render a creation date, or a dash when the API omitted it."""
    return '-' if created is None else f'{created:%Y-%m-%d}'


def format_window(seconds: int) -> str:
    """Render a preview window length as a compact duration."""
    if seconds < _SECONDS_PER_MINUTE:
        return f'{seconds}s'
    if seconds < _SECONDS_PER_HOUR:
        return f'{seconds // _SECONDS_PER_MINUTE}m'
    return f'{seconds // _SECONDS_PER_HOUR}h'


def resolve_name_pattern(pattern: str, names: Sequence[str]) -> list[str]:
    """Filter plain names through the log group resolution ladder.

    Delegates to :func:`resolve_group_pattern` so the dashboard browser's ``/``
    behaves exactly like the group browser's rather than growing a second
    matcher with its own rules.
    """
    stand_ins = [
        LogGroupInfo(name=name, arn='', stored_bytes=None, retention_days=None, created=None) for name in names
    ]
    return [info.name for info in resolve_group_pattern(pattern, stand_ins)]


def selection_status(*, visible: int, total: int, selected: int, cap: int) -> str:
    """Render the status line under a picker table."""
    counts = f'{visible} of {total}' if visible != total else f'{total}'
    return f'{counts} shown  ·  {selected}/{cap} selected'


class Debounce:
    """Collapse a burst of requests into one deferred call.

    Each ``schedule`` cancels the pending call, so walking a list fires the
    callback once the cursor rests rather than once per keystroke.
    """

    def __init__(self, owner: Widget, delay: float = DEBOUNCE_SECONDS) -> None:
        """Time the callback on ``owner``'s message pump."""
        self._owner = owner
        self.delay = delay
        self._timer: Timer | None = None
        self._pending: Callable[[], None] | None = None

    @property
    def pending(self) -> bool:
        """Whether a call is waiting to fire."""
        return self._pending is not None

    def schedule(self, callback: Callable[[], None]) -> None:
        """Replace any waiting call with this one."""
        self.cancel()
        self._pending = callback
        self._timer = self._owner.set_timer(self.delay, self._fire)

    def cancel(self) -> None:
        """Drop any waiting call."""
        if self._timer is not None:
            self._timer.stop()
        self._timer = None
        self._pending = None

    def flush(self) -> None:
        """Run the waiting call now instead of on the timer."""
        pending = self._pending
        self.cancel()
        if pending is not None:
            pending()

    def _fire(self) -> None:
        pending = self._pending
        self._pending = None
        self._timer = None
        if pending is not None:
            pending()


class FilterInput(Input):
    """The ``/`` filter box, which cancels on Escape instead of popping the view."""

    DEFAULT_CSS = """
    FilterInput {
        height: 1;
        padding: 0 1;
        border: none;
        background: $boost;
        display: none;
    }
    """

    class Cancelled(Message):
        """Escape was pressed while the filter had focus."""

    def on_key(self, event: events.Key) -> None:
        """Turn Escape into a cancellation the host view can act on."""
        if event.key == 'escape':
            event.stop()
            event.prevent_default()
            self.post_message(self.Cancelled())


class Picker(Horizontal):
    """A filterable table beside a detail pane."""

    DEFAULT_CSS = """
    Picker {
        height: 1fr;
    }
    Picker > #picker_list {
        width: 3fr;
    }
    Picker > #picker_detail {
        width: 2fr;
        padding: 0 1;
        background: $surface;
    }
    Picker #picker_status {
        height: 1;
        background: $panel;
        padding: 0 1;
    }
    Picker #picker_table {
        height: 1fr;
    }
    """

    def __init__(self, columns: Sequence[PickerColumn], *, placeholder: str) -> None:
        """Build a picker over the given columns, with a filter placeholder."""
        super().__init__()
        self._columns = tuple(columns)
        self._placeholder = placeholder
        self._detail: Text | str = ''

    def compose(self) -> ComposeResult:
        """Yield the filter box, table, status line, and detail pane.

        Yields:
            The left-hand list column followed by the right-hand detail pane.
        """
        with Vertical(id='picker_list'):
            yield FilterInput(placeholder=self._placeholder, id='picker_filter')
            yield DataTable(id='picker_table', cursor_type='row', zebra_stripes=True)
            yield Label('', id='picker_status')
        with VerticalScroll(id='picker_detail'):
            yield Static('', id='picker_detail_body')

    def on_mount(self) -> None:
        """Declare the table columns."""
        table = self.table
        for column in self._columns:
            table.add_column(column.label, key=column.key, width=column.width)

    @property
    def table(self) -> DataTable[Any]:
        """The list table."""
        return self.query_one('#picker_table', DataTable)

    @property
    def filter_input(self) -> FilterInput:
        """The ``/`` filter box."""
        return self.query_one('#picker_filter', FilterInput)

    @property
    def filter_value(self) -> str:
        """The current filter text."""
        return self.filter_input.value

    def open_filter(self) -> None:
        """Show the filter box and focus it."""
        box = self.filter_input
        box.display = True
        box.focus()

    def close_filter(self) -> None:
        """Hide the filter box and return focus to the table."""
        self.filter_input.display = False
        self.table.focus()

    def set_status(self, text: str) -> None:
        """Replace the status line under the table."""
        self.query_one('#picker_status', Label).update(text)

    def show_detail(self, content: Text | str) -> None:
        """Replace the detail pane's contents."""
        self._detail = content
        self.query_one('#picker_detail_body', Static).update(content)

    def detail_text(self) -> str:
        """The detail pane's contents as plain text, for assertions and logs."""
        return self._detail.plain if isinstance(self._detail, Text) else self._detail
