"""A dismissable reference of the keys and ``:`` commands available in a view.

The shell builds the two lists from the active screen's bindings and the command
registry, so this screen only renders what it is handed.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import Static


class WhichKeyScreen(ModalScreen[None]):
    """A dismissable reference of key bindings and ``:`` commands."""

    DEFAULT_CSS = """
    WhichKeyScreen {
        align: center middle;
        background: $background 60%;
    }
    WhichKeyScreen > Static {
        width: auto;
        max-width: 80%;
        height: auto;
        padding: 1 2;
        border: round $accent;
        background: $panel;
    }
    """

    BINDINGS: ClassVar[Sequence[Binding]] = [Binding('escape,comma,q,question_mark', 'dismiss', 'Close')]  # type: ignore[assignment]

    def __init__(self, keys: list[tuple[str, str]], commands: list[tuple[str, str]]) -> None:
        """Show the given key bindings and command summaries."""
        super().__init__()
        self._keys = keys
        self._commands = commands

    def compose(self) -> ComposeResult:
        """Render the reference panel.

        Yields:
            A single Static holding the keys-and-commands reference.
        """
        key_lines = '\n'.join(f'  [b]{key:<8}[/] {description}' for key, description in self._keys)
        command_lines = '\n'.join(f'  [b]:{name:<7}[/] {summary}' for name, summary in self._commands)
        body = f'[b]Keys[/]\n{key_lines}\n\n[b]Commands[/]\n{command_lines}\n\n[dim]esc to close[/]'
        yield Static(body)
