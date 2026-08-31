"""A side panel counting what the loaded events actually contain.

Answers "what is even in these logs" without knowing the payload schema, which
is otherwise a question you answer by reading rows one at a time. Selecting a
value turns it into the filter that would have found it.

The counting itself lives in :mod:`tail_cw.query.facets` and runs off the
message loop; this widget only renders what it is handed.
"""

from __future__ import annotations

from collections.abc import Sequence

from rich.text import Text
from textual.message import Message
from textual.widgets import OptionList
from textual.widgets.option_list import Option

from tail_cw.query.facets import NULL_LABEL, FieldFacet
from tail_cw.text import shorten

PANEL_WIDTH = 30
"""Cells the panel takes from the table. Wide enough for a value and its count."""

MIN_TERMINAL_WIDTH = 120
"""Terminal width under which the panel stays closed.

Matches the width at which the column budget already drops the stream column: a
terminal short enough to shed a column has nothing to spare for a panel, and the
message column is what needs those cells.
"""

_COUNT_WIDTH = 7
_VALUE_INDENT = 2


class FacetSelected(Message):
    """A field value was chosen, to be applied as a record-field filter.

    Lives beside the widget rather than inside it: a nested class here is
    rewritten by the runtime type checker in a way that breaks the widget.

    Attributes:
        field: Dotted payload field path.
        value: The value selected, verbatim.
    """

    def __init__(self, field: str, value: str) -> None:
        """Name the field and the value the reader picked."""
        super().__init__()
        self.field = field
        self.value = value


class FacetsPanel(OptionList):
    """Field counts for the loaded events, each value selectable as a filter."""

    DEFAULT_CSS = """
    FacetsPanel {
        border: none;
        padding: 0 1;
        background: $surface;
    }
    """

    def __init__(self, **kwargs: object) -> None:
        """Build an empty panel; the counts arrive through :meth:`set_facets`."""
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._selectable: dict[str, tuple[str, str]] = {}
        """Option id to the field and value it stands for, replaced whole on every render."""

    def set_message(self, message: str) -> None:
        """Show one line in place of the counts."""
        self._selectable = {}
        self.clear_options()
        self.add_options([Option(Text(message, style='dim'), disabled=True)])

    def set_facets(self, facets: Sequence[FieldFacet], *, width: int | None = None) -> None:
        """Replace the counts shown, keeping the field order it was given.

        Args:
            facets: Fields to render, in the order to render them.
            width: Cells a row may take. Defaults to the panel's own content
                width, which is what a wrapped count needs it to be.
        """
        room = width if width is not None else (self.content_size.width or PANEL_WIDTH)
        self._selectable = {}
        options: list[Option] = []
        for facet in facets:
            if not facet.values:
                continue
            options.append(Option(Text(facet.path, style='bold'), disabled=True))
            options.extend(self._value_options(facet, room))
        self.clear_options()
        self.add_options(options or [Option(Text('no payload fields', style='dim'), disabled=True)])

    def _value_options(self, facet: FieldFacet, width: int) -> list[Option]:
        options = []
        for index, value in enumerate(facet.values):
            key = f'facet-{facet.path}-{index}'
            # A record without the field is a fact worth counting and not a filter
            # anyone can express, so its row is shown and cannot be selected.
            absent = value.value == NULL_LABEL
            if not absent:
                self._selectable[key] = (facet.path, value.value)
            options.append(Option(_value_row(value.value, value.count, width), id=key, disabled=absent))
        return options

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """Turn a selected value into a filter request for the screen."""
        event.stop()
        chosen = self._selectable.get(event.option.id or '')
        if chosen is not None:
            self.post_message(FacetSelected(*chosen))


def _value_row(value: str, count: int, width: int) -> Text:
    counted = f'{count:,}'
    room = max(1, width - _VALUE_INDENT - _COUNT_WIDTH - 1)
    # A wrapped row puts the count on its own line and doubles the panel's height.
    row = Text(' ' * _VALUE_INDENT, no_wrap=True, overflow='crop')
    row.append(shorten(value, room).ljust(room))
    row.append(f' {counted:>{_COUNT_WIDTH}}', style='dim')
    return row
