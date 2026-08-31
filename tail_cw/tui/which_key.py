"""A dismissable reference of the keys and ``:`` commands available in a view.

The shell builds the two lists from the active screen's bindings and the command
registry, so this screen only renders what it is handed.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

TWO_COLUMN_WIDTH = 96
"""Terminal width at which the keys and the commands stand side by side.

Below it they stack, which doubles the height and is what the panel scrolls for.
"""


class WhichKeyScreen(ModalScreen[None]):
    """A dismissable reference of key bindings and ``:`` commands.

    The reference outgrows an 80x24 terminal, so the body scrolls and the hint
    line stays pinned rather than being the first thing cut off.
    """

    DEFAULT_CSS = """
    WhichKeyScreen {
        align: center middle;
        background: $background 60%;
    }
    WhichKeyScreen #which-key-body {
        width: 90%;
        height: auto;
        max-height: 80%;
        padding: 1 2;
        border: round $accent;
        background: $panel;
    }
    WhichKeyScreen #which-key-columns {
        width: auto;
        height: auto;
    }
    WhichKeyScreen .which-key-column {
        width: auto;
        height: auto;
        padding-right: 3;
    }
    WhichKeyScreen #which-key-hint {
        width: 90%;
        content-align: center middle;
        color: $text-muted;
        background: $panel;
    }
    """

    BINDINGS: ClassVar[Sequence[Binding]] = [  # type: ignore[assignment]
        Binding('escape,comma,q,question_mark', 'dismiss', 'Close'),
        Binding('down,j', 'scroll(1)', 'Down', show=False),
        Binding('up,k', 'scroll(-1)', 'Up', show=False),
        Binding('pagedown,space', 'page(1)', 'Page down', show=False),
        Binding('pageup', 'page(-1)', 'Page up', show=False),
    ]

    def __init__(self, keys: list[tuple[str, str]], commands: list[tuple[str, str]]) -> None:
        """Show the given key bindings and command summaries."""
        super().__init__()
        self._keys = keys
        self._commands = commands

    def compose(self) -> ComposeResult:
        """Render the reference panel.

        Yields:
            The scrolling body, then the pinned hint line.
        """
        keys = _section('Keys', [(key, description) for key, description in self._keys])
        commands = _section('Commands', [(f':{name}', summary) for name, summary in self._commands])
        with VerticalScroll(id='which-key-body'):
            if self.app.size.width >= TWO_COLUMN_WIDTH:
                with Horizontal(id='which-key-columns'):
                    yield Static(keys, classes='which-key-column')
                    yield Static(commands, classes='which-key-column')
            else:
                yield Static(f'{keys}\n\n{commands}')
        yield Static('esc to close · j/k or ↑/↓ to scroll', id='which-key-hint')

    def on_mount(self) -> None:
        """Focus the body, so the scroll keys reach it without a click."""
        self.query_one('#which-key-body', VerticalScroll).focus()

    def action_scroll(self, delta: int) -> None:
        """Scroll the reference by one line."""
        self.query_one('#which-key-body', VerticalScroll).scroll_relative(y=delta, animate=False)

    def action_page(self, delta: int) -> None:
        """Scroll the reference by one screenful."""
        body = self.query_one('#which-key-body', VerticalScroll)
        body.scroll_relative(y=delta * max(1, body.size.height - 1), animate=False)


def _section(title: str, rows: Sequence[tuple[str, str]]) -> str:
    width = max((len(key) for key, _ in rows), default=0)
    lines = '\n'.join(f'  [b]{key:<{width}}[/]  {description}' for key, description in rows)
    return f'[b]{title}[/]\n{lines}'
