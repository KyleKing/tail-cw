"""Tests for the group browser: listing, filtering, multi-select, and previews."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from textual.widgets import DataTable

from tail_cw.aws.log_groups import LogGroupInfo
from tail_cw.cli import Session
from tail_cw.config import load_config
from tail_cw.preview import GroupPreview
from tail_cw.query.patterns import MessagePattern
from tail_cw.tui.groups_screen import (
    NO_GROUP_SERVICE,
    NO_PREVIEW_SERVICE,
    GroupsScreen,
    render_preview,
)
from tail_cw.tui.navigation import NavTarget, ViewKind
from tail_cw.tui.picker import (
    Picker,
    format_created,
    format_retention,
    format_window,
    humanize_bytes,
    selection_status,
)
from tail_cw.tui.shell import MAX_SELECTED_GROUPS, ShellScreen, ShellServices, TailCWApp

_NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
_TICK = 0.01


def _group(name: str, *, stored: int | None = 2048, retention: int | None = 14) -> LogGroupInfo:
    return LogGroupInfo(
        name=name,
        arn=f'arn:{name}',
        stored_bytes=stored,
        retention_days=retention,
        created=_NOW - timedelta(days=30),
    )


_GROUPS = [
    _group('/aws/lambda/api'),
    _group('/aws/lambda/api-worker', stored=None, retention=None),
    _group('/ecs/web', stored=1_500_000),
]

_PREVIEW = GroupPreview(
    log_group='/aws/lambda/api',
    event_count=412,
    window_seconds=900,
    patterns=[
        MessagePattern(key='INFO request completed status=<n>', count=318, example='INFO request completed status=200'),
        MessagePattern(key='ERROR Timeout connecting to <str>', count=19, example='ERROR Timeout connecting to db'),
    ],
)


class _StubScreen(ShellScreen):
    """Stands in for the views other agents own, so navigation is observable."""


def _session() -> Session:
    return Session(start=_NOW - timedelta(hours=1), end=_NOW)


def _app(services: ShellServices, session: Session | None = None, *, debounce: float = _TICK) -> TailCWApp:
    def build_screen(target: NavTarget) -> ShellScreen:
        """Stand in for tail_cw.tui.views so only the group browser is exercised."""
        if target.kind is ViewKind.GROUPS:
            return GroupsScreen(debounce_seconds=debounce)
        return _StubScreen()

    return TailCWApp(
        load_config(),
        session if session is not None else _session(),
        build_screen=build_screen,
        services=services,
        target=NavTarget(kind=ViewKind.GROUPS, label='groups'),
    )


async def _settle(app: TailCWApp, pilot) -> None:
    """Let workers finish and short debounce timers fire."""
    for _ in range(4):
        await app.workers.wait_for_complete()
        await pilot.pause()
        await asyncio.sleep(_TICK)


def _screen(app: TailCWApp) -> GroupsScreen:
    screen = app.screen
    assert isinstance(screen, GroupsScreen)
    return screen


def _rows(screen: GroupsScreen) -> list[list[str]]:
    table = screen.query_one(Picker).table
    return [[str(cell) for cell in table.get_row_at(index)] for index in range(table.row_count)]


@pytest.mark.parametrize(
    ('value', 'expected'),
    [(None, '-'), (0, '0 B'), (512, '512 B'), (2048, '2.0 KB'), (1_500_000, '1.4 MB'), (5 * 1024**5, '5.0 PB')],
)
def test_humanize_bytes(value: int | None, expected: str) -> None:
    assert humanize_bytes(value) == expected


def test_format_retention_and_created() -> None:
    assert format_retention(None) == 'never'
    assert format_retention(7) == '7d'
    assert format_created(None) == '-'
    assert format_created(_NOW) == '2026-07-24'


@pytest.mark.parametrize(('seconds', 'expected'), [(30, '30s'), (900, '15m'), (7200, '2h')])
def test_format_window(seconds: int, expected: str) -> None:
    assert format_window(seconds) == expected


def test_selection_status_names_the_filtered_subset() -> None:
    assert selection_status(visible=3, total=3, selected=0, cap=10) == '3 shown  ·  0/10 selected'
    assert selection_status(visible=1, total=3, selected=2, cap=10) == '1 of 3 shown  ·  2/10 selected'


def test_render_preview_shows_counts_and_shapes() -> None:
    rendered = render_preview(_PREVIEW).plain
    assert '412 events, last 15m' in rendered
    assert '318  INFO request completed status=<n>' in rendered


def test_render_preview_handles_an_empty_group() -> None:
    empty = GroupPreview(log_group='/x', event_count=0, window_seconds=900, patterns=[])
    assert 'No events in this window' in render_preview(empty).plain


async def test_list_populates_and_publishes_names_for_completion() -> None:
    calls: list[int] = []

    def list_groups() -> list[LogGroupInfo]:
        calls.append(1)
        return list(_GROUPS)

    app = _app(ShellServices(list_groups=list_groups))
    async with app.run_test(size=(140, 40)) as pilot:
        await _settle(app, pilot)
        screen = _screen(app)
        assert calls == [1]
        assert screen.visible_groups == [info.name for info in _GROUPS]
        assert app.session.group_names == [info.name for info in _GROUPS]
        assert _rows(screen)[1] == ['', '/aws/lambda/api-worker', '-', 'never', '2026-06-24']


async def test_filter_runs_the_resolution_ladder() -> None:
    app = _app(ShellServices(list_groups=lambda: list(_GROUPS)))
    async with app.run_test(size=(140, 40)) as pilot:
        await _settle(app, pilot)
        screen = _screen(app)
        picker = screen.query_one(Picker)

        await pilot.press('slash')
        await pilot.pause()
        assert picker.filter_input.has_focus

        picker.filter_input.value = '/aws/lambda/'
        await pilot.pause()
        assert screen.visible_groups == ['/aws/lambda/api', '/aws/lambda/api-worker']

        picker.filter_input.value = '/aws/lambda/api'
        await pilot.pause()
        assert screen.visible_groups == ['/aws/lambda/api']

        picker.filter_input.value = '*web*'
        await pilot.pause()
        assert screen.visible_groups == ['/ecs/web']

        picker.filter_input.value = 'nothing-here'
        await pilot.pause()
        assert screen.visible_groups == []
        assert picker.detail_text() == 'No group matches this filter'


async def test_filter_submit_keeps_and_escape_clears() -> None:
    app = _app(ShellServices(list_groups=lambda: list(_GROUPS)))
    async with app.run_test(size=(140, 40)) as pilot:
        await _settle(app, pilot)
        screen = _screen(app)
        picker = screen.query_one(Picker)

        await pilot.press('slash')
        picker.filter_input.value = '/ecs'
        await pilot.pause()
        await pilot.press('enter')
        await pilot.pause()
        assert picker.filter_input.display is False
        assert screen.visible_groups == ['/ecs/web']

        await pilot.press('slash')
        await pilot.press('escape')
        await pilot.pause()
        assert not picker.filter_value
        assert screen.visible_groups == [info.name for info in _GROUPS]


async def test_space_toggles_selection_and_marks_the_row() -> None:
    app = _app(ShellServices(list_groups=lambda: list(_GROUPS)))
    async with app.run_test(size=(140, 40)) as pilot:
        await _settle(app, pilot)
        screen = _screen(app)

        await pilot.press('space')
        await pilot.pause()
        assert screen.selected_groups == ['/aws/lambda/api']
        assert app.session.selected_groups == ['/aws/lambda/api']
        assert _rows(screen)[0][0] == '●'

        await pilot.press('space')
        await pilot.pause()
        assert screen.selected_groups == []
        assert not _rows(screen)[0][0]


async def test_selection_survives_a_filter_and_clears_on_c() -> None:
    app = _app(ShellServices(list_groups=lambda: list(_GROUPS)))
    async with app.run_test(size=(140, 40)) as pilot:
        await _settle(app, pilot)
        screen = _screen(app)
        picker = screen.query_one(Picker)

        await pilot.press('space')
        picker.filter_input.value = '/ecs'
        await pilot.pause()
        assert screen.selected_groups == ['/aws/lambda/api']

        await pilot.press('c')
        await pilot.pause()
        assert screen.selected_groups == []
        assert app.session.selected_groups == []


async def test_selection_stops_at_the_live_tail_cap() -> None:
    many = [_group(f'/aws/lambda/fn-{index:02d}') for index in range(12)]
    app = _app(ShellServices(list_groups=lambda: list(many)))
    async with app.run_test(size=(140, 40)) as pilot:
        await _settle(app, pilot)
        screen = _screen(app)
        table = screen.query_one(Picker).table

        for index in range(12):
            table.move_cursor(row=index)
            screen.action_toggle_select()

        assert len(screen.selected_groups) == MAX_SELECTED_GROUPS
        assert screen.selected_groups[-1] == '/aws/lambda/fn-09'


async def test_toggle_is_a_no_op_on_an_empty_list() -> None:
    app = _app(ShellServices(list_groups=list))
    async with app.run_test(size=(140, 40)) as pilot:
        await _settle(app, pilot)
        screen = _screen(app)
        screen.action_toggle_select()
        assert screen.selected_groups == []
        assert screen.highlighted_group() is None


async def test_enter_opens_logs_for_the_highlighted_group() -> None:
    app = _app(ShellServices(list_groups=lambda: list(_GROUPS)))
    async with app.run_test(size=(140, 40)) as pilot:
        await _settle(app, pilot)
        await pilot.press('enter')
        await pilot.pause()
        target = app.nav.stack[-1]
        assert target.kind is ViewKind.LOGS
        assert target.payload == ('/aws/lambda/api',)
        assert target.label == 'logs /aws/lambda/api'


async def test_t_streams_the_selection_live() -> None:
    app = _app(ShellServices(list_groups=lambda: list(_GROUPS)))
    async with app.run_test(size=(140, 40)) as pilot:
        await _settle(app, pilot)
        screen = _screen(app)
        table = screen.query_one(Picker).table

        await pilot.press('space')
        table.move_cursor(row=2)
        await pilot.pause()
        screen.action_toggle_select()
        await pilot.press('t')
        await pilot.pause()

        target = app.nav.stack[-1]
        assert target.kind is ViewKind.LOGS
        assert target.payload == ('/aws/lambda/api', '/ecs/web')
        assert target.label == 'tail 2 groups'


async def test_open_without_a_row_warns_instead_of_navigating() -> None:
    app = _app(ShellServices(list_groups=list))
    async with app.run_test(size=(140, 40)) as pilot:
        await _settle(app, pilot)
        _screen(app).action_open_logs()
        await pilot.pause()
        assert app.nav.stack[-1].kind is ViewKind.GROUPS


async def test_preview_renders_for_the_highlighted_group() -> None:
    requested: list[str] = []

    def preview_group(name: str) -> GroupPreview:
        requested.append(name)
        return GroupPreview(log_group=name, event_count=412, window_seconds=900, patterns=_PREVIEW.patterns)

    app = _app(ShellServices(list_groups=lambda: list(_GROUPS), preview_group=preview_group))
    async with app.run_test(size=(140, 40)) as pilot:
        await _settle(app, pilot)

        detail = _screen(app).query_one(Picker).detail_text()
        assert requested == ['/aws/lambda/api']
        assert '412 events, last 15m' in detail
        assert '318  INFO request completed status=<n>' in detail


async def test_preview_debounces_a_burst_into_one_call() -> None:
    requested: list[str] = []

    def preview_group(name: str) -> GroupPreview:
        requested.append(name)
        return GroupPreview(log_group=name, event_count=1, window_seconds=900, patterns=[])

    app = _app(ShellServices(list_groups=lambda: list(_GROUPS), preview_group=preview_group), debounce=30.0)
    async with app.run_test(size=(140, 40)) as pilot:
        await _settle(app, pilot)
        screen = _screen(app)
        assert requested == []

        await pilot.press('down')
        await pilot.press('down')
        await pilot.pause()
        assert requested == []
        assert screen._preview_debounce is not None
        assert screen._preview_debounce.pending

        screen._preview_debounce.flush()
        await _settle(app, pilot)
        assert requested == ['/ecs/web']


async def test_a_cached_preview_is_not_refetched() -> None:
    requested: list[str] = []

    def preview_group(name: str) -> GroupPreview:
        requested.append(name)
        return GroupPreview(log_group=name, event_count=7, window_seconds=900, patterns=[])

    app = _app(ShellServices(list_groups=lambda: list(_GROUPS), preview_group=preview_group))
    async with app.run_test(size=(140, 40)) as pilot:
        await _settle(app, pilot)

        await pilot.press('down')
        await _settle(app, pilot)
        await pilot.press('up')
        await _settle(app, pilot)
        assert requested == ['/aws/lambda/api', '/aws/lambda/api-worker']


async def test_refresh_view_resamples_after_the_window_moves() -> None:
    requested: list[str] = []

    def preview_group(name: str) -> GroupPreview:
        requested.append(name)
        return GroupPreview(log_group=name, event_count=7, window_seconds=900, patterns=[])

    app = _app(ShellServices(list_groups=lambda: list(_GROUPS), preview_group=preview_group))
    async with app.run_test(size=(140, 40)) as pilot:
        await _settle(app, pilot)
        assert requested == ['/aws/lambda/api']

        app.set_window(_NOW - timedelta(hours=6), _NOW)
        await _settle(app, pilot)
        assert requested == ['/aws/lambda/api', '/aws/lambda/api']


async def test_a_failing_preview_leaves_the_view_usable() -> None:
    def preview_group(name: str) -> GroupPreview:
        msg = f'boom {name}'
        raise RuntimeError(msg)

    app = _app(ShellServices(list_groups=lambda: list(_GROUPS), preview_group=preview_group))
    async with app.run_test(size=(140, 40)) as pilot:
        await _settle(app, pilot)
        assert _screen(app).visible_groups == [info.name for info in _GROUPS]


async def test_a_failing_list_leaves_the_view_usable() -> None:
    def list_groups() -> list[LogGroupInfo]:
        raise RuntimeError('no credentials')

    app = _app(ShellServices(list_groups=list_groups))
    async with app.run_test(size=(140, 40)) as pilot:
        await _settle(app, pilot)
        assert _screen(app).visible_groups == []


async def test_missing_services_explain_themselves() -> None:
    app = _app(ShellServices())
    async with app.run_test(size=(140, 40)) as pilot:
        await _settle(app, pilot)
        assert _screen(app).query_one(Picker).detail_text() == NO_GROUP_SERVICE

    app = _app(ShellServices(list_groups=lambda: list(_GROUPS)))
    async with app.run_test(size=(140, 40)) as pilot:
        await _settle(app, pilot)
        assert _screen(app).query_one(Picker).detail_text() == NO_PREVIEW_SERVICE


async def test_view_commands_reload_select_and_clear() -> None:
    calls: list[int] = []

    def list_groups() -> list[LogGroupInfo]:
        calls.append(1)
        return list(_GROUPS)

    app = _app(ShellServices(list_groups=list_groups))
    async with app.run_test(size=(140, 40)) as pilot:
        await _settle(app, pilot)
        screen = _screen(app)
        assert 'reload' in screen.commands()
        assert screen.nav_siblings() == []

        assert screen.run_view_command('reload', '') is True
        await _settle(app, pilot)
        assert calls == [1, 1]

        assert screen.run_view_command('select', '/aws/lambda/') is True
        assert screen.selected_groups == ['/aws/lambda/api', '/aws/lambda/api-worker']

        assert screen.run_view_command('select', 'missing') is True
        assert screen.selected_groups == ['/aws/lambda/api', '/aws/lambda/api-worker']

        assert screen.run_view_command('clear', '') is True
        assert screen.selected_groups == []
        assert screen.run_view_command('unknown', '') is False


async def test_select_refuses_past_the_cap() -> None:
    many = [_group(f'/aws/lambda/fn-{index:02d}') for index in range(12)]
    app = _app(ShellServices(list_groups=lambda: list(many)))
    async with app.run_test(size=(140, 40)) as pilot:
        await _settle(app, pilot)
        screen = _screen(app)
        screen.run_view_command('select', '/aws/lambda/')
        assert len(screen.selected_groups) == MAX_SELECTED_GROUPS
        screen.run_view_command('select', '/aws/lambda/fn-11')
        assert len(screen.selected_groups) == MAX_SELECTED_GROUPS


async def test_a_session_selection_is_adopted_on_mount() -> None:
    session = _session()
    session.selected_groups = ['/ecs/web']
    app = _app(ShellServices(list_groups=lambda: list(_GROUPS)), session)
    async with app.run_test(size=(140, 40)) as pilot:
        await _settle(app, pilot)
        screen = _screen(app)
        assert screen.selected_groups == ['/ecs/web']
        assert _rows(screen)[2][0] == '●'


async def test_the_table_holds_focus_after_the_command_line_closes() -> None:
    app = _app(ShellServices(list_groups=lambda: list(_GROUPS)))
    async with app.run_test(size=(140, 40)) as pilot:
        await _settle(app, pilot)
        screen = _screen(app)
        await pilot.press('colon')
        await pilot.pause()
        screen.restore_focus()
        await pilot.pause()
        assert screen.query_one(Picker).table.has_focus
        assert isinstance(screen.query_one(Picker).table, DataTable)
