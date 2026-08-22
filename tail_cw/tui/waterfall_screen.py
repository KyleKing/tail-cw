"""One X-Ray trace as a waterfall: what ran, how long, and what it ran inside.

The bar is the point. A tree can show the hierarchy, but only a bar placed against the
trace's own start says which of two sibling calls the parent was actually waiting on,
which is the question a slow request raises.

Layout is :mod:`tail_cw.waterfall`; this file picks a width and paints it.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable, Label

from tail_cw.aws.xray import XRaySpan, XRayTrace
from tail_cw.tui.shell import ShellScreen
from tail_cw.tui.span_detail import SpanDetailScreen
from tail_cw.waterfall import WaterfallRow, indented_name, render_bar, waterfall_rows

FAULT_MARKER = '✗'
"""Marks a faulted span, in a column of its own like the log table's severity glyph.

Colour alone cannot carry this: under ``NO_COLOR`` the red row renders as dim, which
makes the one span that failed the least prominent thing on the screen. Its own column
rather than a prefix on the name, because a prefix displaces the indentation and a
nested span then reads as a root.
"""

MIN_BAR_WIDTH = 12
MIN_NAME_WIDTH = 16
MAX_NAME_WIDTH = 38
MAX_SERVICE_WIDTH = 18
DURATION_WIDTH = 9
_MARKER_WIDTH = 1
_TABLE_PADDING = 10
"""Cell padding and the cursor gutter the DataTable spends outside the four columns."""
_MS_PER_SECOND = 1000.0


def column_widths(total: int) -> tuple[int, int, int]:
    """Split ``total`` terminal columns into the span, service, and timeline widths.

    The bar is the point of this view, so it takes whatever the others do not need, and
    the others shrink before it does. At 80 columns a fixed 38-wide name and 18-wide
    service left the timeline 12 columns, which cannot tell two sibling calls apart.
    """
    fixed = _TABLE_PADDING + _MARKER_WIDTH + DURATION_WIDTH
    spare = max(0, total - fixed - MIN_BAR_WIDTH)
    name = max(MIN_NAME_WIDTH, min(MAX_NAME_WIDTH, spare // 2))
    service = max(0, min(MAX_SERVICE_WIDTH, spare - name))
    bar = max(MIN_BAR_WIDTH, total - fixed - name - service)
    return name, service, bar


def duration_label(span: XRaySpan) -> str:
    """Milliseconds under a second, seconds above it, ``open`` while still running."""
    elapsed = span.duration_ms
    if elapsed is None:
        return 'open'
    if elapsed >= _MS_PER_SECOND:
        return f'{elapsed / _MS_PER_SECOND:.2f}s'
    return f'{elapsed:.0f}ms'


def row_style(row: WaterfallRow) -> str:
    """Colour a row by what it says, so the picture reads without the columns.

    An inferred span is dimmed because its timings are the caller's view of the work
    rather than the work's own account of itself.
    """
    if row.span.is_fault or row.span.is_error:
        return 'red'
    if row.span.is_inferred:
        return 'dim'
    if row.on_slowest_chain:
        return 'bold'
    return ''


def _plural(count: int, noun: str) -> str:
    return f'{count} {noun}' if count == 1 else f'{count} {noun}s'


def trace_headline(trace: XRayTrace) -> str:
    """One line naming the trace, for the status bar under the table."""
    if not trace.spans:
        return f'{trace.trace_id}: X-Ray returned no segments'
    widest = max(trace.spans, key=lambda span: span.duration_ms or 0.0)
    truncated = ' · truncated by X-Ray' if trace.limit_exceeded else ''
    return (
        f'{_plural(len(trace.spans), "span")} · {_plural(len(trace.service_names), "service")} · '
        f'{_plural(trace.error_count, "error")} · widest {widest.name} at {duration_label(widest)}{truncated}'
    )


class WaterfallScreen(ShellScreen):
    """Fetches one trace from X-Ray and draws its spans against the trace's own width."""

    DEFAULT_CSS = """
    WaterfallScreen #waterfall {
        height: 1fr;
        width: 100%;
    }
    WaterfallScreen #waterfall_status {
        height: 1;
        width: 100%;
        background: $panel;
        color: $text;
        padding: 0 1;
    }
    """

    BINDINGS: ClassVar[Sequence[Binding]] = [
        Binding('r', 'reload', 'Reload'),
        Binding('s', 'toggle_inferred', 'Inferred'),
        Binding('enter', 'show_span', 'Span detail'),
    ]

    def __init__(self, trace_id: str) -> None:
        """Show the X-Ray trace named by ``trace_id``."""
        super().__init__()
        self._trace_id = trace_id
        self._trace: XRayTrace | None = None
        self._show_inferred = True

    def compose_content(self) -> ComposeResult:  # ruff: ignore[no-self-use]
        """Yield the span table and its status line."""
        yield DataTable(id='waterfall', cursor_type='row', zebra_stripes=False)
        yield Label('', id='waterfall_status')

    def on_mount(self) -> None:
        """Draw the breadcrumb, prepare the columns, then fetch the trace."""
        super().on_mount()
        table = self.query_one('#waterfall', DataTable)
        table.add_columns('!', 'span', 'service', 'duration', 'timeline')
        # A table with no focus swallows j, k, and the bindings the footer advertises.
        table.focus()
        self.action_reload()

    def action_reload(self) -> None:
        """Fetch the trace again."""
        self.run_worker(self._load(), name='waterfall', group='waterfall', exclusive=True)

    @on(DataTable.RowSelected, '#waterfall')
    def on_row_selected(self, event: DataTable.RowSelected) -> None:
        """Open the detail for the row Enter was pressed on.

        DataTable binds Enter itself, so the screen's own binding never sees the key and
        the footer's hint would otherwise do nothing. The log view solved this first.
        """
        event.stop()
        self.action_show_span()

    def action_show_span(self) -> None:
        """Show what the row could not fit: the statement, the fault, the annotations."""
        rows = self._visible_rows()
        table = self.query_one('#waterfall', DataTable)
        if not rows or not (0 <= table.cursor_row < len(rows)):
            return
        self.app.push_screen(SpanDetailScreen(rows[table.cursor_row].span))

    def action_toggle_inferred(self) -> None:
        """Hide or show the segments X-Ray synthesized rather than received."""
        self._show_inferred = not self._show_inferred
        self._draw()

    async def _load(self) -> None:
        fetch = self.shell.services.fetch_xray_trace
        if fetch is None:
            self._set_status('No AWS credentials, so X-Ray is unavailable')
            return
        self._set_status(f'Fetching {self._trace_id}...')
        try:
            self._trace = await fetch(self._trace_id)
        except Exception as err:
            self._set_status(f'Failed: {err}')
            return
        self._draw()

    def _visible_rows(self) -> list[WaterfallRow]:
        if self._trace is None:
            return []
        rows = waterfall_rows(self._trace)
        if self._show_inferred:
            return rows
        return [row for row in rows if not row.span.is_inferred]

    def _draw(self) -> None:
        table = self.query_one('#waterfall', DataTable)
        table.clear()
        rows = self._visible_rows()
        name_width, service_width, bar_width = column_widths(self.size.width)
        for row in rows:
            style = row_style(row)
            marker = FAULT_MARKER if row.span.is_fault or row.span.is_error else ''
            table.add_row(
                Text(marker, style=style or 'red'),
                Text(indented_name(row, width=name_width), style=style),
                Text(row.span.service_name[:service_width], style=style),
                Text(duration_label(row.span), style=style, justify='right'),
                Text(render_bar(row, width=bar_width), style=style or 'cyan'),
            )
        if self._trace is None:
            self._set_status(f'{self._trace_id}: nothing loaded')
            return
        hidden = len(waterfall_rows(self._trace)) - len(rows)
        suffix = f' · {hidden} inferred hidden' if hidden else ''
        self._set_status(f'{trace_headline(self._trace)}{suffix}')

    def _set_status(self, message: str) -> None:
        """Show one line of plain text.

        Never a bare string: an X-Ray error names its trace and a Polars one its file in
        brackets, which Rich reads as a closing tag and raises ``MarkupError`` for.
        """
        self.query_one('#waterfall_status', Label).update(Text(message))
