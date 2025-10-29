"""Modal screen for displaying detailed log event information.

This module provides RecordDetailScreen, a Textual ModalScreen that shows
the full details of a selected log event. The modal includes:

- All LogEvent fields with labels
- Full (non-truncated) message content
- Automatic JSON parsing and pretty-printing for JSONL messages
- Keyboard shortcuts for navigation (Escape, q to close)
- Scrollable content area for long messages

The modal is designed for keyboard-first navigation and follows
accessibility best practices.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from tail_cw.aws.client import LogEvent
from tail_cw.tui.log_viewer import format_log_event_detail_with_json


class RecordDetailScreen(ModalScreen[None]):
    """Modal screen for displaying full log event details.

    This screen shows comprehensive information about a single log event,
    including all fields and the complete message. If the message is JSON,
    it displays both the raw and pretty-printed versions.

    Keyboard shortcuts:
        - Escape: Close the modal
        - q: Close the modal (alternative)
        - c: Copy to clipboard (placeholder for future)

    Args:
        log_event: The LogEvent instance to display

    Example:
        >>> event = LogEvent(...)
        >>> app.push_screen(RecordDetailScreen(event))
    """

    CSS = """
    RecordDetailScreen {
        align: center middle;
    }

    #dialog {
        width: 80%;
        max-width: 120;
        height: auto;
        max-height: 80%;
        border: thick $accent;
        padding: 1 2;
        background: $panel;
    }

    #content {
        height: auto;
        max-height: 30;
        overflow-y: auto;
        margin-bottom: 1;
    }

    #buttons {
        layout: horizontal;
        align: center middle;
        height: auto;
    }
    """

    BINDINGS: ClassVar[Sequence[Binding]] = [  # type: ignore[assignment]
        Binding('escape', 'dismiss_modal', 'Close', show=True),
        Binding('q', 'dismiss_modal', 'Close', show=False),
        Binding('c', 'copy_to_clipboard', 'Copy', show=True),
    ]

    def __init__(self, log_event: LogEvent) -> None:
        """Initialize the modal with a log event.

        Args:
            log_event: The log event to display in detail
        """
        super().__init__()
        self._log_event = log_event

    def compose(self) -> ComposeResult:
        """Build the modal UI.

        Yields:
            Container with dialog containing:
                - Static widget with formatted event details (scrollable)
                - Button container with Close button
        """
        with Container(id='dialog'), Vertical():
            yield Static(self._format_content(), id='content')
            with Container(id='buttons'):
                yield Button('Close', id='close', variant='primary')

    def _format_content(self) -> str:
        """Format the log event for display.

        Uses format_log_event_detail_with_json to include JSON parsing
        if the message is JSON.

        Returns:
            Formatted multi-line string with event details
        """
        return format_log_event_detail_with_json(self._log_event)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button click events.

        Args:
            event: The button press event
        """
        if event.button.id == 'close':
            self.dismiss()

    def action_dismiss_modal(self) -> None:
        """Handle keyboard shortcuts to close the modal.

        Bound to Escape and q keys.
        """
        self.dismiss()

    def action_copy_to_clipboard(self) -> None:
        """Placeholder for copy to clipboard functionality.

        This will be implemented in a future phase. Would require a clipboard
        library (e.g., pyperclip), which would be a new dependency.

        Shows a notification to inform the user this is coming soon.
        """
        self.app.notify('Copy to clipboard coming soon', severity='information')
