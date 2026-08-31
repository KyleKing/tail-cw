"""Tests for the field-count panel."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from tail_cw.query.facets import NULL_LABEL, FacetValue, FieldFacet, is_identifier_like, worth_showing
from tail_cw.tui.facets_panel import FacetSelected, FacetsPanel


def _facet(path: str, values: list[tuple[str, int]], *, distinct: int | None = None) -> FieldFacet:
    counted = tuple(FacetValue(value=value, count=count) for value, count in values)
    present = sum(count for value, count in values if value != NULL_LABEL)
    return FieldFacet(
        path=path,
        values=counted,
        present=present,
        distinct=distinct if distinct is not None else len(counted),
    )


class _Host(App[None]):
    def __init__(self, facets: list[FieldFacet]) -> None:
        super().__init__()
        self._facets = facets
        self.selected: list[tuple[str, str]] = []

    def compose(self) -> ComposeResult:  # ruff: ignore[no-self-use]
        yield FacetsPanel(id='facets')

    def on_mount(self) -> None:
        panel = self.query_one(FacetsPanel)
        panel.set_facets(self._facets)
        panel.focus()

    def on_facet_selected(self, event: FacetSelected) -> None:
        self.selected.append((event.field, event.value))


@pytest.mark.parametrize(
    ('present', 'distinct', 'expected'),
    [
        # A trace id lists one value per record with a count of one each.
        (13, 13, True),
        (13, 3, False),
        # Too few records for the cardinality to say anything.
        (2, 2, False),
    ],
)
def test_a_field_with_a_value_per_record_makes_a_poor_facet(present, distinct, expected):
    facet = FieldFacet(path='trace_id', values=(), present=present, distinct=distinct)

    assert is_identifier_like(facet) is expected


def test_worth_showing_keeps_the_identifier_when_it_is_all_there_is():
    """An empty panel tells the reader less than a useless one."""
    only = _facet('trace_id', [(f'trace-{index}', 1) for index in range(9)])

    assert worth_showing([only]) == [only]


@pytest.mark.asyncio
async def test_the_panel_lists_each_field_then_its_values_with_counts():
    facets = [_facet('level', [('info', 847), ('error', 12)])]
    app = _Host(facets)

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        prompts = [str(option.prompt) for option in app.query_one(FacetsPanel)._options]

        assert prompts[0].strip() == 'level'
        assert 'info' in prompts[1]
        assert '847' in prompts[1]


@pytest.mark.asyncio
async def test_a_row_never_wraps_its_count_onto_a_second_line():
    """A wrapped row put the count on its own line and doubled the panel's height."""
    app = _Host([_facet('level', [('a-very-long-value-that-will-not-fit-at-all', 12)])])

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        rendered = str(app.query_one(FacetsPanel)._options[1].prompt)

        assert '\n' not in rendered
        assert '12' in rendered


@pytest.mark.asyncio
async def test_choosing_a_value_asks_for_it_as_a_filter():
    app = _Host([_facet('level', [('info', 8), ('error', 2)])])

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await pilot.press('down', 'enter')
        await pilot.pause()

        assert app.selected == [('level', 'info')]


@pytest.mark.asyncio
async def test_the_records_missing_a_field_are_counted_but_not_selectable():
    """No filter expresses "absent", so offering it as one would do nothing."""
    app = _Host([_facet('level', [(NULL_LABEL, 5)])])

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await pilot.press('down', 'enter')
        await pilot.pause()

        assert app.selected == []
        assert NULL_LABEL in str(app.query_one(FacetsPanel)._options[1].prompt)


@pytest.mark.asyncio
async def test_the_panel_says_so_when_there_is_nothing_to_count():
    app = _Host([])

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()

        assert 'no payload fields' in str(app.query_one(FacetsPanel)._options[0].prompt)
