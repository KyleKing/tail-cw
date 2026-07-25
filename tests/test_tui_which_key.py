"""Tests for the which-key reference screen."""

from __future__ import annotations

import pytest
from textual.app import App
from textual.widgets import Static

from tail_cw.tui.which_key import WhichKeyScreen


class _Host(App[None]):
    def on_mount(self) -> None:
        self.push_screen(WhichKeyScreen([('q', 'Quit')], [('help', 'List the available commands')]))


@pytest.mark.asyncio
async def test_which_key_lists_keys_and_commands() -> None:
    app = _Host()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, WhichKeyScreen)
        body = str(app.screen.query_one(Static).render())
        assert 'Quit' in body
        assert 'List the available commands' in body


@pytest.mark.asyncio
async def test_escape_dismisses_the_reference() -> None:
    app = _Host()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await pilot.press('escape')
        await pilot.pause()
        assert not isinstance(app.screen, WhichKeyScreen)
