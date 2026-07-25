"""Tests for the dashboard browser: listing, filtering, previews, and opening."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from tail_cw.aws.dashboards import (
    AlarmWidget,
    Dashboard,
    DashboardSummary,
    LogWidget,
    MetricWidget,
    TextWidget,
    UnknownWidget,
    Widget,
    WidgetLayout,
)
from tail_cw.cli import Session
from tail_cw.config import load_config
from tail_cw.tui.dashboards_screen import (
    NO_DETAIL_SERVICE,
    NO_LIST_SERVICE,
    DashboardsScreen,
    render_dashboard_detail,
    widget_label,
)
from tail_cw.tui.navigation import NavTarget, ViewKind
from tail_cw.tui.picker import Picker, resolve_name_pattern
from tail_cw.tui.shell import ShellScreen, ShellServices, TailCWApp

_NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
_TICK = 0.01

_SUMMARIES = [
    DashboardSummary(name='prod-overview', arn='arn:prod', size=4096),
    DashboardSummary(name='prod-lambda', arn='arn:lambda', size=1024),
    DashboardSummary(name='staging', arn='arn:staging', size=512),
]

_DASHBOARD = Dashboard(
    name='prod-overview',
    widgets=[
        MetricWidget(layout=WidgetLayout(), title='api latency', view='timeSeries', metrics=[]),
        LogWidget(layout=WidgetLayout(), title='', query="SOURCE '/aws/lambda/api'"),
        TextWidget(layout=WidgetLayout(), markdown='# Runbook\nsteps'),
        AlarmWidget(layout=WidgetLayout(), title='paging', alarms=['arn:alarm']),
        UnknownWidget(layout=WidgetLayout(), widget_type='custom'),
    ],
)


class _StubScreen(ShellScreen):
    """Stands in for the dashboard view another agent owns."""


def _app(services: ShellServices, *, debounce: float = _TICK) -> TailCWApp:
    def build_screen(target: NavTarget) -> ShellScreen:
        """Stand in for tail_cw.tui.views so only the dashboard browser is exercised."""
        if target.kind is ViewKind.DASHBOARDS:
            return DashboardsScreen(debounce_seconds=debounce)
        return _StubScreen()

    return TailCWApp(
        load_config(),
        Session(start=_NOW - timedelta(hours=1), end=_NOW),
        build_screen=build_screen,
        services=services,
        target=NavTarget(kind=ViewKind.DASHBOARDS, label='dashboards'),
    )


async def _settle(app: TailCWApp, pilot) -> None:
    """Let workers finish and short debounce timers fire."""
    for _ in range(4):
        await app.workers.wait_for_complete()
        await pilot.pause()
        await asyncio.sleep(_TICK)


def _screen(app: TailCWApp) -> DashboardsScreen:
    screen = app.screen
    assert isinstance(screen, DashboardsScreen)
    return screen


@pytest.mark.parametrize(
    ('widget', 'expected'),
    [
        (MetricWidget(layout=WidgetLayout(), title='api', view='bar', metrics=[]), ('bar', 'api')),
        (MetricWidget(layout=WidgetLayout(), title='', view='bar', metrics=[]), ('bar', 'untitled metric')),
        (LogWidget(layout=WidgetLayout(), title='errors', query=''), ('logs', 'errors')),
        (LogWidget(layout=WidgetLayout(), title='', query=''), ('logs', 'untitled query')),
        (AlarmWidget(layout=WidgetLayout(), title='', alarms=[]), ('alarm', 'untitled alarm')),
        (TextWidget(layout=WidgetLayout(), markdown='\n## Notes\n'), ('text', 'Notes')),
        (TextWidget(layout=WidgetLayout(), markdown=''), ('text', 'empty text')),
        (UnknownWidget(layout=WidgetLayout(), widget_type='custom'), ('custom', 'unsupported widget')),
    ],
)
def test_widget_label(widget: Widget, expected: tuple[str, str]) -> None:
    assert widget_label(widget) == expected


def test_render_dashboard_detail_lists_widget_titles() -> None:
    rendered = render_dashboard_detail(_DASHBOARD).plain
    assert 'prod-overview' in rendered
    assert '5 widgets' in rendered
    assert 'api latency' in rendered
    assert 'Runbook' in rendered


def test_render_dashboard_detail_handles_an_empty_dashboard() -> None:
    assert 'no widgets' in render_dashboard_detail(Dashboard(name='bare')).plain


def test_resolve_name_pattern_reuses_the_group_ladder() -> None:
    names = ['prod-overview', 'prod-lambda', 'staging']
    assert resolve_name_pattern('', names) == names
    assert resolve_name_pattern('prod-', names) == ['prod-overview', 'prod-lambda']
    assert resolve_name_pattern('prod-lambda', names) == ['prod-lambda']
    assert resolve_name_pattern('*lambda*', names) == ['prod-lambda']
    assert resolve_name_pattern('none', names) == []


async def test_list_populates_and_publishes_names_for_completion() -> None:
    calls: list[int] = []

    def list_dashboards() -> list[DashboardSummary]:
        calls.append(1)
        return list(_SUMMARIES)

    app = _app(ShellServices(list_dashboards=list_dashboards))
    async with app.run_test(size=(140, 40)) as pilot:
        await _settle(app, pilot)
        screen = _screen(app)
        assert calls == [1]
        assert screen.visible_dashboards == ['prod-overview', 'prod-lambda', 'staging']
        assert app.session.dashboard_names == ['prod-overview', 'prod-lambda', 'staging']
        table = screen.query_one(Picker).table
        assert [str(cell) for cell in table.get_row_at(0)] == ['prod-overview', '4.0 KB']


async def test_filter_narrows_the_list() -> None:
    app = _app(ShellServices(list_dashboards=lambda: list(_SUMMARIES)))
    async with app.run_test(size=(140, 40)) as pilot:
        await _settle(app, pilot)
        screen = _screen(app)
        picker = screen.query_one(Picker)

        await pilot.press('slash')
        await pilot.pause()
        assert picker.filter_input.has_focus

        picker.filter_input.value = 'prod-'
        await pilot.pause()
        assert screen.visible_dashboards == ['prod-overview', 'prod-lambda']

        picker.filter_input.value = 'none'
        await pilot.pause()
        assert screen.visible_dashboards == []
        assert picker.detail_text() == 'No dashboard matches this filter'

        picker.filter_input.value = 'prod-'
        await pilot.pause()
        await pilot.press('enter')
        await pilot.pause()
        assert picker.filter_input.display is False
        assert screen.visible_dashboards == ['prod-overview', 'prod-lambda']

        await pilot.press('slash')
        await pilot.press('escape')
        await pilot.pause()
        assert not picker.filter_value
        assert screen.visible_dashboards == ['prod-overview', 'prod-lambda', 'staging']


async def test_enter_opens_the_highlighted_dashboard() -> None:
    app = _app(ShellServices(list_dashboards=lambda: list(_SUMMARIES)))
    async with app.run_test(size=(140, 40)) as pilot:
        await _settle(app, pilot)
        await pilot.press('down')
        await pilot.pause()
        await pilot.press('enter')
        await pilot.pause()
        target = app.nav.stack[-1]
        assert target.kind is ViewKind.DASHBOARD
        assert target.label == 'prod-lambda'
        assert target.payload == ('prod-lambda',)


async def test_open_without_a_row_warns_instead_of_navigating() -> None:
    app = _app(ShellServices(list_dashboards=list))
    async with app.run_test(size=(140, 40)) as pilot:
        await _settle(app, pilot)
        _screen(app).action_open_dashboard()
        await pilot.pause()
        assert app.nav.stack[-1].kind is ViewKind.DASHBOARDS


async def test_detail_pane_previews_widget_titles() -> None:
    requested: list[str] = []

    def load_dashboard(name: str) -> Dashboard:
        requested.append(name)
        return Dashboard(name=name, widgets=list(_DASHBOARD.widgets))

    app = _app(ShellServices(list_dashboards=lambda: list(_SUMMARIES), load_dashboard=load_dashboard))
    async with app.run_test(size=(140, 40)) as pilot:
        await _settle(app, pilot)
        screen = _screen(app)
        assert requested == ['prod-overview']
        assert 'api latency' in screen.query_one(Picker).detail_text()


async def test_detail_debounces_a_burst_into_one_call() -> None:
    requested: list[str] = []

    def load_dashboard(name: str) -> Dashboard:
        requested.append(name)
        return Dashboard(name=name)

    app = _app(ShellServices(list_dashboards=lambda: list(_SUMMARIES), load_dashboard=load_dashboard), debounce=30.0)
    async with app.run_test(size=(140, 40)) as pilot:
        await _settle(app, pilot)
        screen = _screen(app)
        assert requested == []

        await pilot.press('down')
        await pilot.press('down')
        await pilot.pause()
        assert requested == []

        assert screen._detail_debounce is not None
        screen._detail_debounce.flush()
        await _settle(app, pilot)
        assert requested == ['staging']


async def test_a_cached_body_is_not_refetched() -> None:
    requested: list[str] = []

    def load_dashboard(name: str) -> Dashboard:
        requested.append(name)
        return Dashboard(name=name)

    app = _app(ShellServices(list_dashboards=lambda: list(_SUMMARIES), load_dashboard=load_dashboard))
    async with app.run_test(size=(140, 40)) as pilot:
        await _settle(app, pilot)
        screen = _screen(app)

        await pilot.press('down')
        await _settle(app, pilot)
        await pilot.press('up')
        await _settle(app, pilot)
        assert requested == ['prod-overview', 'prod-lambda']

        screen.refresh_view()
        await _settle(app, pilot)
        assert requested == ['prod-overview', 'prod-lambda']


async def test_failures_leave_the_view_usable() -> None:
    def load_dashboard(name: str) -> Dashboard:
        msg = f'boom {name}'
        raise RuntimeError(msg)

    app = _app(ShellServices(list_dashboards=lambda: list(_SUMMARIES), load_dashboard=load_dashboard))
    async with app.run_test(size=(140, 40)) as pilot:
        await _settle(app, pilot)
        screen = _screen(app)
        assert screen.visible_dashboards == ['prod-overview', 'prod-lambda', 'staging']

    def list_dashboards() -> list[DashboardSummary]:
        raise RuntimeError('no credentials')

    app = _app(ShellServices(list_dashboards=list_dashboards))
    async with app.run_test(size=(140, 40)) as pilot:
        await _settle(app, pilot)
        assert _screen(app).visible_dashboards == []


async def test_missing_services_explain_themselves() -> None:
    app = _app(ShellServices())
    async with app.run_test(size=(140, 40)) as pilot:
        await _settle(app, pilot)
        assert _screen(app).query_one(Picker).detail_text() == NO_LIST_SERVICE

    app = _app(ShellServices(list_dashboards=lambda: list(_SUMMARIES)))
    async with app.run_test(size=(140, 40)) as pilot:
        await _settle(app, pilot)
        assert _screen(app).query_one(Picker).detail_text() == NO_DETAIL_SERVICE


async def test_siblings_cycle_the_listed_dashboards() -> None:
    app = _app(ShellServices(list_dashboards=lambda: list(_SUMMARIES)))
    async with app.run_test(size=(140, 40)) as pilot:
        await _settle(app, pilot)
        screen = _screen(app)
        assert [target.label for target in screen.nav_siblings()] == ['prod-overview', 'prod-lambda', 'staging']
        assert all(target.kind is ViewKind.DASHBOARD for target in screen.nav_siblings())


async def test_reload_command_refetches() -> None:
    calls: list[int] = []

    def list_dashboards() -> list[DashboardSummary]:
        calls.append(1)
        return list(_SUMMARIES)

    app = _app(ShellServices(list_dashboards=list_dashboards))
    async with app.run_test(size=(140, 40)) as pilot:
        await _settle(app, pilot)
        screen = _screen(app)
        assert 'reload' in screen.commands()
        assert screen.run_view_command('reload', '') is True
        await _settle(app, pilot)
        assert calls == [1, 1]
        assert screen.run_view_command('unknown', '') is False


async def test_the_table_holds_focus_after_the_command_line_closes() -> None:
    app = _app(ShellServices(list_dashboards=lambda: list(_SUMMARIES)))
    async with app.run_test(size=(140, 40)) as pilot:
        await _settle(app, pilot)
        screen = _screen(app)
        await pilot.press('colon')
        await pilot.pause()
        screen.restore_focus()
        await pilot.pause()
        assert screen.query_one(Picker).table.has_focus
