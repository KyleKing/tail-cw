"""One span's full record, for what a waterfall row has no columns for.

The row shows a name, a service, a duration, and a bar. A segment document also carries
the statement a SQL span ran, the exception that faulted it, and its annotations, and
none of that fits on a row. Without this screen it was fetched and unreachable.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Label, Static

from tail_cw.aws.xray import XRaySpan

_MS_PER_SECOND = 1000.0


def span_lines(span: XRaySpan) -> Iterator[tuple[str, str]]:
    """Yield the label and value of every field the span actually carries.

    Absent fields are skipped rather than shown empty, because a segment document omits
    most of them and a screen of blanks reads as missing data rather than as absence.
    """
    yield 'span', span.span_id
    yield 'trace', span.trace_id
    if span.parent_span_id:
        yield 'parent', span.parent_span_id
    yield 'service', span.service_name
    yield 'started', f'{span.start_time.isoformat()}'
    elapsed = span.duration_ms
    yield 'duration', 'still open' if elapsed is None else f'{elapsed:.2f}ms ({elapsed / _MS_PER_SECOND:.3f}s)'
    for label, value in (
        ('origin', span.origin),
        ('namespace', span.namespace),
        ('http status', span.http_status),
        ('statement', span.sql_url),
        ('error', span.error_message),
    ):
        if value is not None:
            yield label, str(value)
    flags = [
        name for name, on in (('fault', span.is_fault), ('error', span.is_error), ('throttled', span.is_throttle)) if on
    ]
    if flags:
        yield 'flags', ', '.join(flags)
    if span.is_inferred:
        yield 'inferred', 'X-Ray synthesized this span, so its timings bound the caller, not the work'
    for key, value in span.annotations:
        yield f'annotation.{key}', value


class SpanDetailScreen(ModalScreen[None]):
    """Shows one span's record, dismissed with escape."""

    DEFAULT_CSS = """
    SpanDetailScreen {
        align: center middle;
        background: $background 60%;
    }
    SpanDetailScreen > VerticalScroll {
        width: 80%;
        max-width: 110;
        height: auto;
        max-height: 80%;
        border: round $accent;
        background: $panel;
        padding: 1 2;
    }
    """

    BINDINGS: ClassVar[Sequence[Binding]] = [  # type: ignore[assignment]
        Binding('escape,q,enter', 'dismiss', 'Close'),
    ]

    def __init__(self, span: XRaySpan) -> None:
        """Show ``span``."""
        super().__init__()
        self._span = span

    def compose(self) -> ComposeResult:
        """Yield the span's name and one row per field it carries."""
        with VerticalScroll():
            yield Label(Text(self._span.name, style='bold'))
            yield Static(_render(self._span))
            yield Label(Text('\nesc to close', style='dim'))


def _render(span: XRaySpan) -> Text:
    body = Text()
    for label, value in span_lines(span):
        body.append(f'\n{label:>12}  ', style='dim')
        body.append(value)
    return body
