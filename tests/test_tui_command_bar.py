"""Tests for the ``:`` command line widget itself."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from tail_cw.tui.command_bar import CommandLine


class _LineHarness(App[None]):
    def __init__(self, completer) -> None:
        super().__init__()
        self.line = CommandLine(completer=completer)

    def compose(self) -> ComposeResult:
        yield self.line


def _focusable(app: _LineHarness) -> bool:
    return bool(app.line.can_focus)


@pytest.mark.asyncio
async def test_command_line_tab_cycles_completions() -> None:
    app = _LineHarness(lambda _value: ['stat Average', 'stat Sum', 'stat Minimum'])
    async with app.run_test() as pilot:
        await pilot.pause()
        app.line._complete()
        assert app.line.value == 'stat Average'
        app.line._complete()
        assert app.line.value == 'stat Sum'


@pytest.mark.asyncio
async def test_command_line_history_walks_backwards() -> None:
    app = _LineHarness(lambda _value: [])
    async with app.run_test() as pilot:
        await pilot.pause()
        app.line.remember('range 1h')
        app.line.remember('stat p99')
        app.line._history_step(-1)
        assert app.line.value == 'stat p99'
        app.line._history_step(-1)
        assert app.line.value == 'range 1h'


@pytest.mark.asyncio
async def test_an_open_command_line_has_a_row_to_show_what_is_typed() -> None:
    """An Input keeps its border under :focus, which left a one-row prompt no content row.

    It read and ran what was typed while showing nothing, so the only honest
    check is that the content region exists.
    """
    app = _LineHarness(lambda _value: [])
    async with app.run_test(size=(80, 24)) as pilot:
        app.line.open()
        await pilot.pause()
        await pilot.press('r')
        await pilot.pause()

        assert app.line.has_focus
        assert app.line.content_region.height >= 1
        assert app.line.value == 'r'


@pytest.mark.asyncio
async def test_a_closed_command_line_is_not_in_the_focus_chain() -> None:
    app = _LineHarness(lambda _value: [])
    async with app.run_test() as pilot:
        await pilot.pause()

        assert _focusable(app) is False
        assert not app.line.has_focus, 'a hidden input in the chain swallows every keystroke'

        app.line.open()
        await pilot.pause()
        assert _focusable(app) is True

        app.line.close()
        await pilot.pause()
        assert _focusable(app) is False
