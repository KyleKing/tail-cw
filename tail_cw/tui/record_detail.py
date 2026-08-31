"""Modal screen showing one log event in full.

The parsed payload leads and the raw line folds away behind ``r``. For a real
structlog record the two are the same information twice, and putting the raw copy
first meant scrolling past it to reach the readable one.

The payload is syntax-highlighted, because a wall of one flat colour separates
neither a key from a value nor a string from a number.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar

from rich.console import RenderableType
from rich.json import JSON
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from tail_cw.aws.events import LogEvent
from tail_cw.tui.log_viewer import format_log_event_detail_with_json, parse_jsonl_message

_LABEL_STYLE = 'bold'


class RecordDetailScreen(ModalScreen[None]):
    """One log event in full, parsed payload first.

    Keyboard shortcuts:
        - Escape, q: close
        - r: show or hide the raw line the payload was decoded from
        - c: copy every field to the clipboard (OSC 52)
    """

    CSS = """
    RecordDetailScreen {
        align: center middle;
    }

    RecordDetailScreen #dialog {
        width: 90%;
        max-width: 120;
        height: auto;
        max-height: 85%;
        border: round $accent;
        padding: 1 2;
        background: $panel;
    }

    RecordDetailScreen #detail-body {
        width: 100%;
        height: auto;
        max-height: 100%;
    }

    RecordDetailScreen .detail-section {
        height: auto;
        margin-bottom: 1;
    }

    RecordDetailScreen #detail-hint {
        width: 100%;
        color: $text-muted;
        background: $panel;
    }
    """

    BINDINGS: ClassVar[Sequence[Binding]] = [  # type: ignore[assignment]
        Binding('escape', 'dismiss_modal', 'Close', show=True),
        Binding('q', 'dismiss_modal', 'Close', show=False),
        Binding('r', 'toggle_raw', 'Raw line', show=True),
        Binding('c', 'copy_to_clipboard', 'Copy', show=True),
    ]

    def __init__(self, log_event: LogEvent) -> None:
        """Show one log event.

        Args:
            log_event: The log event to display in detail
        """
        super().__init__()
        self._log_event = log_event
        self._payload = parse_jsonl_message(log_event.message)

    def compose(self) -> ComposeResult:
        """Build the modal.

        Yields:
            The scrolling dialog, then the pinned hint line.
        """
        with Vertical(id='dialog'):
            with VerticalScroll(id='detail-body'):
                yield Static(field_summary(self._log_event), classes='detail-section', id='detail-fields')
                yield Static(self._body(), classes='detail-section', id='detail-payload')
                raw = Static(Text(self._log_event.message), classes='detail-section', id='detail-raw')
                # A payload that decoded is the same information as the raw line, so the
                # raw copy starts folded; a line that never was JSON has nothing else.
                raw.display = self._payload is None
                yield raw
            yield Static(self._hint(), id='detail-hint')

    def _body(self) -> RenderableType:
        if self._payload is None:
            return Text('')
        return JSON(self._payload)

    def _hint(self) -> Text:
        raw_hint = '' if self._payload is None else ' · r for the raw line'
        return Text(f'esc to close · c to copy{raw_hint}', style='dim')

    def action_toggle_raw(self) -> None:
        """Show or hide the raw line the payload was decoded from."""
        if self._payload is None:
            return
        raw = self.query_one('#detail-raw', Static)
        raw.display = not raw.display

    def action_dismiss_modal(self) -> None:
        """Close the modal."""
        self.dismiss()

    def action_copy_to_clipboard(self) -> None:
        """Copy every field, and both forms of the message, to the clipboard.

        Uses Textual's built-in OSC 52 support, which needs a terminal emulator
        that handles the escape sequence.
        """
        self.app.copy_to_clipboard(format_log_event_detail_with_json(self._log_event))
        self.app.notify('Copied event details to clipboard', severity='information')


def field_summary(event: LogEvent) -> Text:
    """Render an event's own fields as labelled lines, without its message."""
    ingestion = event.ingestion_time.isoformat() if event.ingestion_time else 'N/A'
    rows = (
        ('Timestamp', event.timestamp.isoformat()),
        ('Log Group', event.log_group),
        ('Log Stream', event.log_stream),
        ('Ingestion Time', ingestion),
    )
    width = max(len(label) for label, _ in rows)
    text = Text()
    for index, (label, value) in enumerate(rows):
        if index:
            text.append('\n')
        text.append(f'{label:<{width}}', style=_LABEL_STYLE)
        text.append(f'  {value}')
    return text
