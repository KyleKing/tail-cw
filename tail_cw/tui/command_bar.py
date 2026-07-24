"""A vim-style ``:`` command line for the dashboard.

Opens on ``:``, completes command names and argument values with Tab, and walks
prior commands with Up/Down. It only collects and dispatches text; the command
registry and handlers live in the app so they can act on its state directly.
"""

from __future__ import annotations

from collections.abc import Callable

from textual import events
from textual.widgets import Input

Completer = Callable[[str], list[str]]


class CommandLine(Input):
    """A single-line command prompt with Tab completion and history."""

    DEFAULT_CSS = """
    CommandLine {
        dock: bottom;
        height: 1;
        border: none;
        padding: 0 1;
        background: $panel;
        display: none;
    }
    CommandLine:focus {
        border: none;
    }
    """

    def __init__(self, *, completer: Completer) -> None:
        """Create the command line with a value completer."""
        super().__init__(id='command_line')
        self._completer = completer
        self._history: list[str] = []
        self._history_index = 0
        self._completions: list[str] = []
        self._completion_index = 0

    def open(self) -> None:
        """Show the command line and take focus."""
        self.display = True
        self.value = ''
        self._reset_completion()
        self.focus()

    def close(self) -> None:
        """Hide the command line."""
        self.value = ''
        self.display = False

    def remember(self, command: str) -> None:
        """Record an executed command in history."""
        if command and (not self._history or self._history[-1] != command):
            self._history.append(command)
        self._history_index = len(self._history)

    def _reset_completion(self) -> None:
        self._completions = []
        self._completion_index = 0

    def on_key(self, event: events.Key) -> None:
        """Handle Tab completion, history navigation, and cancel."""
        if event.key == 'tab':
            event.prevent_default()
            event.stop()
            self._complete()
        elif event.key == 'escape':
            event.stop()
            self.close()
        elif event.key == 'up':
            event.stop()
            self._history_step(-1)
        elif event.key == 'down':
            event.stop()
            self._history_step(1)
        else:
            self._reset_completion()

    def _complete(self) -> None:
        if not self._completions:
            self._completions = self._completer(self.value)
            self._completion_index = 0
        if not self._completions:
            return
        self.value = self._completions[self._completion_index % len(self._completions)]
        self._completion_index += 1
        self.cursor_position = len(self.value)

    def _history_step(self, direction: int) -> None:
        if not self._history:
            return
        self._history_index = max(0, min(len(self._history), self._history_index + direction))
        self.value = self._history[self._history_index] if self._history_index < len(self._history) else ''
        self.cursor_position = len(self.value)
