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
from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable, Label

from tail_cw.aws.xray import XRaySpan, XRayTrace
from tail_cw.tui.shell import ShellScreen
from tail_cw.waterfall import WaterfallRow, indented_name, render_bar, waterfall_rows

BAR_WIDTH = 34
NAME_WIDTH = 38
SERVICE_WIDTH = 18
_MS_PER_SECOND = 1000.0


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


def trace_headline(trace: XRayTrace) -> str:
    """One line naming the trace, for the status bar under the table."""
    if not trace.spans:
        return f'{trace.trace_id}: X-Ray returned no segments'
    widest = max(trace.spans, key=lambda span: span.duration_ms or 0.0)
    truncated = ' · truncated by X-Ray' if trace.limit_exceeded else ''
    return (
        f'{len(trace.spans)} spans · {len(trace.service_names)} services · '
        f'{trace.error_count} errors · widest {widest.name} at {duration_label(widest)}{truncated}'
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
        table.add_columns('span', 'service', 'duration', 'timeline')
        # A table with no focus swallows j, k, and the bindings the footer advertises.
        table.focus()
        self.action_reload()

    def action_reload(self) -> None:
        """Fetch the trace again."""
        self.run_worker(self._load(), name='waterfall', group='waterfall', exclusive=True)

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
        for row in rows:
            style = row_style(row)
            table.add_row(
                Text(indented_name(row, width=NAME_WIDTH), style=style),
                Text(row.span.service_name[:SERVICE_WIDTH], style=style),
                Text(duration_label(row.span), style=style, justify='right'),
                Text(render_bar(row, width=BAR_WIDTH), style=style or 'cyan'),
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
