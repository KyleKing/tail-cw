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
