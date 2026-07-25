"""The dive confirmation: ranked log-group candidates for a dashboard widget.

Dive never jumps straight into a guess. Even one exact ``SOURCE`` match arrives
here pre-selected so the keystroke is uniform, and the list doubles as the
explanation of why tail-cw thinks these groups back the widget. Confirming opens
the shared log view over the session window, carrying the widget's own filter
text in as the starting filter.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, ClassVar, cast

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import Label, OptionList

from tail_cw.aws.dashboards import DiveCandidate, LogWidget, Widget

if TYPE_CHECKING:
    from tail_cw.tui.shell import TailCWApp

_FILTER_RE = re.compile(r'\|\s*filter\s+(?P<expression>[^|]+)', re.IGNORECASE)


def widget_filter_text(widget: Widget) -> str | None:
    """Return the widget's own filter expression, or None when it has none.

    Only Insights log widgets carry query text; a ``filter`` clause in one is the
    closest thing a widget has to the log view's filter pattern.
    """
    match widget:
        case LogWidget():
            found = _FILTER_RE.search(widget.query)
            return found.group('expression').strip() if found else None
        case _:
            return None


def _require_shell(app: App[Any]) -> App[Any]:
    if not (hasattr(app, 'session') and hasattr(app, 'open_logs')):
        msg = 'DiveConfirmScreen requires a TailCWApp host'
        raise TypeError(msg)
    return app


def candidate_label(candidate: DiveCandidate) -> str:
    """Render one candidate as a single row: group, evidence, and event count."""
    match candidate:
        case DiveCandidate(exists=False):
            evidence = 'not found in this account'
        case DiveCandidate(event_count=int() as count):
            evidence = f'{count} events in window'
        case _:
            evidence = 'exists'
    return f'{candidate.log_group}  ·  {candidate.reason}  ·  {evidence}'


class DiveConfirmScreen(ModalScreen[None]):
    """Lists the ranked candidates and waits for ``enter``."""

    DEFAULT_CSS = """
    DiveConfirmScreen {
        align: center middle;
        background: $background 60%;
    }
    DiveConfirmScreen > Vertical, DiveConfirmScreen > OptionList {
        width: 80%;
        max-width: 100;
    }
    DiveConfirmScreen > Label {
        width: 80%;
        max-width: 100;
        padding: 0 1;
        color: $text-muted;
    }
    DiveConfirmScreen > OptionList {
        height: auto;
        max-height: 60%;
        border: round $accent;
        background: $panel;
    }
    """

    BINDINGS: ClassVar[Sequence[Binding]] = [  # type: ignore[assignment]
        Binding('escape,q', 'dismiss', 'Cancel'),
        Binding('j', 'cursor_down', 'Down'),
        Binding('k', 'cursor_up', 'Up'),
    ]

    def __init__(self, widget: Widget, candidates: list[DiveCandidate]) -> None:
        """Show the widget's candidates in ranked order, the first pre-selected."""
        super().__init__()
        self._widget = widget
        self._candidates = list(candidates)

    def compose(self) -> ComposeResult:
        """Render the candidate list.

        Yields:
            A heading, the selectable candidates, and a hint line.
        """
        title = getattr(self._widget, 'title', '') or 'this widget'
        yield Label(f'Dive into which log group for {title}?')
        yield OptionList(*[candidate_label(candidate) for candidate in self._candidates], id='dive_candidates')
        yield Label('[dim]enter opens the logs · esc cancels[/]')

    def on_mount(self) -> None:
        """Pre-select the top-ranked candidate and take focus."""
        options = self.query_one('#dive_candidates', OptionList)
        if self._candidates:
            options.highlighted = 0
        options.focus()

    def action_cursor_down(self) -> None:
        """Move the highlight one candidate down."""
        self.query_one('#dive_candidates', OptionList).action_cursor_down()

    def action_cursor_up(self) -> None:
        """Move the highlight one candidate up."""
        self.query_one('#dive_candidates', OptionList).action_cursor_up()

    @on(OptionList.OptionSelected, '#dive_candidates')
    def _on_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.open_candidate(event.option_index)

    def open_candidate(self, index: int) -> None:
        """Open the log view for the candidate at the given rank."""
        if not 0 <= index < len(self._candidates):
            return
        shell = cast('TailCWApp', _require_shell(self.app))
        if pattern := widget_filter_text(self._widget):
            shell.session.filter_pattern = pattern
        self.dismiss()
        shell.open_logs([self._candidates[index].log_group])
