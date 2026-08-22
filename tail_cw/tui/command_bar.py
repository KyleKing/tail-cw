"""The text inputs that appear on a key and leave when they are done.

:class:`HiddenInput` is the shared behaviour: closed inputs drop out of the
focus chain, because a hidden ``Input`` that stays in it takes the initial focus
and swallows every keystroke the footer advertises. :class:`CommandLine` adds
the vim-style ``:`` prompt on top, with Tab completion and command history.
"""

from __future__ import annotations

from collections.abc import Callable

from textual import events
from textual.widgets import Input

Completer = Callable[[str], list[str]]

PROMPT_CSS = """
    height: 1;
    width: 100%;
    /* Input's own rule sets a tall border and a height of 3, and Input:focus outranks a
       plain type selector, so a prompt that does not restate all of this in its own leaf
       rule renders as an empty three-row box. */
    border: none !important;
    padding: 0 1;
"""
"""Shared rule body every prompt has to repeat in its own selector."""


class HiddenInput(Input):
    """An input that is out of the way, and out of the focus chain, until opened.

    Neither subclass docks, and the command line carries no border. A docked
    input lands on the same row as the docked ``Footer`` or breadcrumb, which
    paints over it, and an ``Input`` carries a border by default, and
    ``Input:focus`` outranks a plain type selector, so the ``:focus`` state has
    to be named too or a one-row command line keeps a border and has no row
    left for its own text: either way the command line reads and runs what is typed into it
    while showing nothing at all.
    """

    def __init__(self, **kwargs: object) -> None:
        """Create the input closed and unfocusable.

        Closed in code rather than in CSS, because a subclass that declares its
        own rule for this type would otherwise decide whether it starts hidden.
        """
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.display = False
        self.can_focus = False

    def open(self) -> None:
        """Show the input and take focus."""
        self.display = True
        self.can_focus = True
        self.focus()

    def close(self) -> None:
        """Clear the input, hide it, and drop out of the focus chain."""
        self.value = ''
        self.display = False
        self.can_focus = False

    def on_key(self, event: events.Key) -> None:
        """Close on Escape, so the key never reaches the screen's own Back binding."""
        if event.key == 'escape':
            event.stop()
            self.close()


class SearchLine(HiddenInput):
    """The ``/`` search box over the current view, one row like a vim prompt."""

    DEFAULT_CSS = f"""
    SearchLine {{
        {PROMPT_CSS}
        background: $panel;
        color: $text;
    }}
    """

    def __init__(self, *, placeholder: str) -> None:
        """Create the search box, closed."""
        super().__init__(id='search_input', placeholder=placeholder)


class CommandLine(HiddenInput):
    """A single-line command prompt with Tab completion and history."""

    DEFAULT_CSS = f"""
    CommandLine {{
        {PROMPT_CSS}
        background: $accent 20%;
        color: $text;
    }}
    """

    def __init__(self, *, completer: Completer) -> None:
        """Create the command line with a value completer, closed."""
        super().__init__(id='command_line', placeholder=': run a command (Tab completes, Enter runs, Esc cancels)')
        self._completer = completer
        self._history: list[str] = []
        self._history_index = 0
        self._completions: list[str] = []
        self._completion_index = 0

    def open(self) -> None:
        """Show the command line, empty, with no completion in progress."""
        self.value = ''
        self._reset_completion()
        super().open()

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
            super().on_key(event)
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
