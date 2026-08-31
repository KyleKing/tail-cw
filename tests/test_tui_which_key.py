"""Tests for the which-key reference screen."""

from __future__ import annotations

import pytest
from textual.app import App
from textual.containers import VerticalScroll
from textual.widgets import Static

from tail_cw.tui.which_key import WhichKeyScreen

_MANY_KEYS = [(f'k{index}', f'Action {index}') for index in range(40)]


class _Host(App[None]):
    def __init__(self, keys=None, commands=None) -> None:
        super().__init__()
        self._keys = keys if keys is not None else [('q', 'Quit')]
        self._commands = commands if commands is not None else [('help', 'List the available commands')]

    def on_mount(self) -> None:
        self.push_screen(WhichKeyScreen(self._keys, self._commands))


def _text(app: App[None]) -> str:
    return '\n'.join(str(static.render()) for static in app.screen.query(Static))


@pytest.mark.asyncio
async def test_which_key_lists_keys_and_commands() -> None:
    app = _Host()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, WhichKeyScreen)
        body = _text(app)
        assert 'Quit' in body
        assert 'List the available commands' in body


@pytest.mark.asyncio
async def test_the_close_hint_survives_a_reference_taller_than_the_terminal() -> None:
    """It was the first line cut off, on the panel whose whole job is discovery."""
    app = _Host(keys=_MANY_KEYS)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        hint = app.screen.query_one('#which-key-hint', Static)

        assert hint.region.height == 1
        assert 'esc to close' in str(hint.render())


@pytest.mark.asyncio
async def test_pressing_j_scrolls_the_reference() -> None:
    app = _Host(keys=_MANY_KEYS)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        body = app.screen.query_one('#which-key-body', VerticalScroll)
        assert body.scroll_offset.y == 0

        await pilot.press('j')
        await pilot.pause()

        assert body.scroll_offset.y > 0


@pytest.mark.asyncio
async def test_a_wide_terminal_puts_the_commands_beside_the_keys() -> None:
    app = _Host(keys=_MANY_KEYS)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        columns = app.screen.query('.which-key-column')

        assert len(columns) == 2
        assert columns[0].region.y == columns[1].region.y


@pytest.mark.asyncio
async def test_escape_dismisses_the_reference() -> None:
    app = _Host()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await pilot.press('escape')
        await pilot.pause()
        assert not isinstance(app.screen, WhichKeyScreen)
