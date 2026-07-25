# ruff: file-ignore[unused-async] - the service fakes conform to awaitable signatures
"""Tests for the dashboard screen: grid sizing, motions, commands, and dive."""

from __future__ import annotations

import functools
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from textual.pilot import Pilot
from textual.widgets import Static

from tail_cw.aws.dashboards import (
    AlarmWidget,
    Dashboard,
    LogWidget,
    MetricWidget,
    TextWidget,
    UnknownWidget,
    WidgetLayout,
)
from tail_cw.aws.log_groups import LogGroupInfo
from tail_cw.aws.metrics import MetricSeries
from tail_cw.cli import Session
from tail_cw.config import load_config
from tail_cw.tui.command_bar import CommandLine
from tail_cw.tui.dashboard_screen import DashboardScreen, _grid_dimensions
from tail_cw.tui.dive_screen import DiveConfirmScreen
from tail_cw.tui.navigation import NavTarget, ViewKind
from tail_cw.tui.plot_widget import PlotChart
from tail_cw.tui.shell import ShellServices, TailCWApp
from tail_cw.tui.views import build_screen
from tail_cw.tui.which_key import WhichKeyScreen

from .asyncsupport import returns

_NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)


def _metric(title: str, *, view: str = 'timeSeries', function_name: str = 'my-fn') -> MetricWidget:
    return MetricWidget(
        layout=WidgetLayout(width=12, height=6),
        title=title,
        view=view,
        metrics=[['AWS/Lambda', 'Invocations', 'FunctionName', function_name, {'id': 'm', 'stat': 'Sum'}]],
        period=300,
    )


def _series(start: datetime) -> list[MetricSeries]:
    timestamps = [start + timedelta(minutes=30 * i) for i in range(6)]
    return [MetricSeries(id='m', label='Invocations', timestamps=timestamps, values=[float(i) for i in range(6)])]


def _session(*, dashboard_names: list[str] | None = None) -> Session:
    return Session(
        start=_NOW - timedelta(hours=3),
        end=_NOW,
        dashboard_names=dashboard_names if dashboard_names is not None else ['demo'],
    )


def _build_app(
    dashboard: Dashboard,
    *,
    session: Session | None = None,
    services: ShellServices | None = None,
    name: str = 'demo',
) -> TailCWApp:
    return TailCWApp(
        load_config(),
        session if session is not None else _session(),
        build_screen=build_screen,
        services=services
        if services is not None
        else ShellServices(load_dashboard=returns(dashboard), fetch_metrics=returns(_series(_NOW))),
        target=NavTarget(kind=ViewKind.DASHBOARD, label=name, payload=(name,)),
    )


async def _settle(app: TailCWApp, pilot: Pilot[None]) -> None:
    for _ in range(4):
        await app.workers.wait_for_complete()
        await pilot.pause()


async def _wait_until(pilot: Pilot[None], predicate: Callable[[], bool], *, tries: int = 40) -> None:
    for _ in range(tries):
        if predicate():
            return
        await pilot.pause()
    msg = 'the expected condition never held'
    raise AssertionError(msg)


def _is_staged(screen: DashboardScreen, index: int) -> bool:
    return screen._staged == [index]


def _screen(app: TailCWApp) -> DashboardScreen:
    screen = app.screen
    assert isinstance(screen, DashboardScreen)
    return screen


@pytest.mark.parametrize(('count', 'expected'), [(1, (1, 1)), (4, (3, 2)), (9, (3, 3)), (12, (4, 3))])
def test_grid_dimensions_fit(count: int, expected: tuple[int, int]) -> None:
    assert _grid_dimensions(count) == expected


@pytest.mark.asyncio
async def test_screen_creates_cells_and_fetches_metrics() -> None:
    dashboard = Dashboard(
        name='demo', widgets=[_metric('one'), _metric('two'), TextWidget(layout=WidgetLayout(), markdown='# hi')]
    )
    calls: list[tuple[datetime, datetime]] = []

    async def fetch(_queries: object, start: datetime, end: datetime) -> list[MetricSeries]:
        calls.append((start, end))
        return _series(start)

    app = _build_app(dashboard, services=ShellServices(load_dashboard=returns(dashboard), fetch_metrics=fetch))
    async with app.run_test(size=(160, 48)) as pilot:
        await _settle(app, pilot)
        assert len(_screen(app)._panels) == 3
        assert len(calls) == 2  # only the two metric widgets fetch


@pytest.mark.asyncio
async def test_first_metric_is_staged_by_default() -> None:
    dashboard = Dashboard(name='demo', widgets=[TextWidget(layout=WidgetLayout(), markdown='# hi'), _metric('one')])
    app = _build_app(dashboard)
    async with app.run_test(size=(160, 48)) as pilot:
        await _settle(app, pilot)
        assert _screen(app)._staged == [1]  # the metric, not the text widget


@pytest.mark.asyncio
async def test_focus_mounts_a_native_plot_chart() -> None:
    app = _build_app(Dashboard(name='demo', widgets=[_metric('one')]))
    async with app.run_test(size=(160, 48)) as pilot:
        await _settle(app, pilot)
        await pilot.press('enter')
        await _wait_until(pilot, lambda: len(list(app.screen.query(PlotChart))) == 1)


@pytest.mark.asyncio
async def test_enter_stages_and_escape_clears() -> None:
    app = _build_app(Dashboard(name='demo', widgets=[_metric('one'), _metric('two')]))
    async with app.run_test(size=(160, 48)) as pilot:
        await _settle(app, pilot)
        screen = _screen(app)
        assert screen._staged == [0]
        await pilot.press('l')  # select the second cell
        await pilot.press('enter')
        await pilot.pause()
        assert screen._staged == [1]
        await pilot.press('escape')
        await pilot.pause()
        assert screen._staged == []


@pytest.mark.asyncio
async def test_escape_pops_the_screen_once_the_stage_is_clear() -> None:
    dashboard = Dashboard(name='demo', widgets=[_metric('one')])
    app = _build_app(dashboard, session=_session(dashboard_names=['demo', 'other']))
    async with app.run_test(size=(160, 48)) as pilot:
        await _settle(app, pilot)
        app.goto(NavTarget(kind=ViewKind.DASHBOARD, label='other', payload=('other',)))
        await _settle(app, pilot)
        assert _screen(app).dashboard_name == 'other'
        await pilot.press('escape')  # clears the stage
        await pilot.pause()
        assert _screen(app)._staged == []
        assert _screen(app).dashboard_name == 'other'
        await pilot.press('escape')  # falls through to the shell
        await _settle(app, pilot)
        assert _screen(app).dashboard_name == 'demo'


@pytest.mark.asyncio
async def test_hjkl_navigates_the_grid() -> None:
    app = _build_app(Dashboard(name='demo', widgets=[_metric(str(i)) for i in range(6)]))
    async with app.run_test(size=(160, 48)) as pilot:
        await _settle(app, pilot)
        screen = _screen(app)
        screen._panels[0].cell.focus()
        await pilot.pause()
        await pilot.press('l')
        await pilot.pause()
        assert screen._focused_panel() is screen._panels[1]
        await pilot.press('j')  # down one row (3 columns)
        await pilot.pause()
        assert screen._focused_panel() is screen._panels[4]


@pytest.mark.asyncio
async def test_bracket_cycles_dashboards() -> None:
    dashboard = Dashboard(name='demo', widgets=[_metric('one')])
    app = _build_app(dashboard, session=_session(dashboard_names=['demo', 'other', 'third']))
    async with app.run_test(size=(160, 48)) as pilot:
        await _settle(app, pilot)
        assert len(_screen(app).nav_siblings()) == 3
        app.goto(NavTarget(kind=ViewKind.DASHBOARD, label='other', payload=('other',)))
        await _settle(app, pilot)
        await pilot.press(']')
        await _settle(app, pilot)
        assert _screen(app).dashboard_name == 'third'
        await pilot.press('[')
        await _settle(app, pilot)
        assert _screen(app).dashboard_name == 'other'


@pytest.mark.asyncio
async def test_panels_command_hides_unmatched_cells() -> None:
    dashboard = Dashboard(name='demo', widgets=[_metric('api errors'), _metric('queue depth')])
    app = _build_app(dashboard)
    async with app.run_test(size=(160, 48)) as pilot:
        await _settle(app, pilot)
        screen = _screen(app)
        app.run_command(screen, 'panels errors')
        await pilot.pause()
        assert [panel.cell.display for panel in screen._panels] == [True, False]
        app.run_command(screen, 'panels all')
        await pilot.pause()
        assert [panel.cell.display for panel in screen._panels] == [True, True]


@pytest.mark.asyncio
async def test_panels_command_keeps_every_cell_when_nothing_matches() -> None:
    dashboard = Dashboard(name='demo', widgets=[_metric('api errors')])
    app = _build_app(dashboard)
    async with app.run_test(size=(160, 48)) as pilot:
        await _settle(app, pilot)
        screen = _screen(app)
        assert screen.run_view_command('panels', 'nothing-matches')
        await pilot.pause()
        assert screen._panels[0].cell.display


@pytest.mark.asyncio
async def test_unknown_command_falls_through_to_the_shell() -> None:
    app = _build_app(Dashboard(name='demo', widgets=[_metric('one')]))
    async with app.run_test(size=(160, 48)) as pilot:
        await _settle(app, pilot)
        assert _screen(app).run_view_command('nonsense', '') is False


@pytest.mark.asyncio
async def test_refresh_view_refetches_after_a_window_change() -> None:
    dashboard = Dashboard(name='demo', widgets=[_metric('one')])
    calls: list[tuple[datetime, datetime]] = []

    async def fetch(_queries: object, start: datetime, end: datetime) -> list[MetricSeries]:
        calls.append((start, end))
        return _series(start)

    app = _build_app(dashboard, services=ShellServices(load_dashboard=returns(dashboard), fetch_metrics=fetch))
    async with app.run_test(size=(160, 48)) as pilot:
        await _settle(app, pilot)
        assert len(calls) == 1
        later = _NOW + timedelta(hours=1)
        app.set_window(later - timedelta(minutes=15), later)
        await _settle(app, pilot)
        assert len(calls) == 2
        assert calls[-1] == (later - timedelta(minutes=15), later)


@pytest.mark.asyncio
async def test_stat_and_period_cycles_refetch_the_focused_panel() -> None:
    dashboard = Dashboard(name='demo', widgets=[_metric('one')])
    app = _build_app(dashboard)
    async with app.run_test(size=(160, 48)) as pilot:
        await _settle(app, pilot)
        screen = _screen(app)
        await pilot.press('s')
        await _settle(app, pilot)
        assert screen._panels[0].stat_override == 'Sum'
        await pilot.press('p')
        await _settle(app, pilot)
        assert screen._panels[0].period_override == 900


@pytest.mark.asyncio
async def test_focus_and_add_commands_stage_panels_by_title() -> None:
    dashboard = Dashboard(name='demo', widgets=[_metric('one'), _metric('two')])
    app = _build_app(dashboard)
    async with app.run_test(size=(160, 48)) as pilot:
        await _settle(app, pilot)
        screen = _screen(app)
        assert screen.run_view_command('focus', 'two')
        await pilot.pause()
        assert screen._staged == [1]
        assert screen.run_view_command('add', 'one')
        await pilot.pause()
        assert screen._staged == [1, 0]
        assert screen.run_view_command('reset', '')
        await pilot.pause()
        assert screen._staged == []


@pytest.mark.asyncio
async def test_commands_offer_panel_titles_as_completions() -> None:
    dashboard = Dashboard(name='demo', widgets=[_metric('api latency')])
    app = _build_app(dashboard)
    async with app.run_test(size=(160, 48)) as pilot:
        await _settle(app, pilot)
        screen = _screen(app)
        assert screen.commands()['focus'].args == ('api latency',)
        assert screen.complete_command('focus ') == ['focus api latency']


@pytest.mark.asyncio
async def test_dive_ranks_candidates_then_opens_the_chosen_group(monkeypatch: pytest.MonkeyPatch) -> None:
    widget = LogWidget(
        layout=WidgetLayout(),
        title='api logs',
        query="SOURCE '/aws/lambda/api' | filter level = 'ERROR' | fields @message",
    )
    dashboard = Dashboard(name='demo', widgets=[widget, _metric('one')])
    opened: list[tuple[list[str], datetime, datetime]] = []

    def fake_open_logs(self: TailCWApp, groups: list[str], *, live: bool = False) -> None:
        del live
        opened.append((list(groups), self.session.start, self.session.end))

    monkeypatch.setattr(TailCWApp, 'open_logs', fake_open_logs)
    services = ShellServices(
        load_dashboard=returns(dashboard),
        fetch_metrics=returns(_series(_NOW)),
        list_groups=returns(
            [LogGroupInfo(name='/aws/lambda/api', arn='arn', stored_bytes=None, retention_days=None, created=None)]
        ),
        count_events=returns(7),
    )
    app = _build_app(dashboard, services=services)
    async with app.run_test(size=(160, 48)) as pilot:
        await _settle(app, pilot)
        screen = _screen(app)
        screen._panels[0].cell.focus()
        await pilot.pause()
        await pilot.press('d')
        await _settle(app, pilot)
        await _wait_until(pilot, lambda: isinstance(app.screen, DiveConfirmScreen))
        await pilot.press('enter')
        await _wait_until(pilot, lambda: bool(opened))
        assert opened == [(['/aws/lambda/api'], _NOW - timedelta(hours=3), _NOW)]
        assert app.session.filter_pattern == "level = 'ERROR'"


@pytest.mark.asyncio
async def test_dive_reports_when_no_candidate_exists() -> None:
    dashboard = Dashboard(name='demo', widgets=[TextWidget(layout=WidgetLayout(), markdown='# hi')])
    app = _build_app(dashboard)
    async with app.run_test(size=(160, 48)) as pilot:
        await _settle(app, pilot)
        screen = _screen(app)
        screen._panels[0].cell.focus()
        await pilot.pause()
        await pilot.press('d')
        await _settle(app, pilot)
        assert isinstance(app.screen, DashboardScreen)


@pytest.mark.asyncio
async def test_missing_dashboard_service_reports_in_the_status_line() -> None:
    app = TailCWApp(
        load_config(),
        _session(),
        build_screen=build_screen,
        services=ShellServices(),
        target=NavTarget(kind=ViewKind.DASHBOARD, label='demo', payload=('demo',)),
    )
    async with app.run_test(size=(160, 48)) as pilot:
        await _settle(app, pilot)
        screen = _screen(app)
        assert screen._panels == []
        assert 'no dashboard pipeline wired' in str(screen.query_one('#dash_status').render())


@pytest.mark.asyncio
async def test_log_widget_cells_render_their_volume_sparkline() -> None:
    widget = LogWidget(layout=WidgetLayout(), title='api logs', query="SOURCE '/aws/lambda/api' | fields @message")
    dashboard = Dashboard(name='demo', widgets=[widget])
    volume_calls: list[str] = []

    async def log_volume(group: str, _start: datetime, _end: datetime) -> list[float]:
        volume_calls.append(group)
        return [1.0, 4.0, 2.0]

    services = ShellServices(load_dashboard=returns(dashboard), log_volume=log_volume)
    app = _build_app(dashboard, services=services)
    async with app.run_test(size=(160, 48)) as pilot:
        await _settle(app, pilot)
        assert volume_calls == ['/aws/lambda/api']


@pytest.mark.asyncio
async def test_metric_fetch_failure_shows_the_error_in_the_cell() -> None:
    dashboard = Dashboard(name='demo', widgets=[_metric('one')])

    async def fetch(*_args: object) -> list[MetricSeries]:
        msg = 'throttled'
        raise RuntimeError(msg)

    app = _build_app(dashboard, services=ShellServices(load_dashboard=returns(dashboard), fetch_metrics=fetch))
    async with app.run_test(size=(160, 48)) as pilot:
        await _settle(app, pilot)
        assert 'throttled' in str(_screen(app)._panels[0].cell.render())


@pytest.mark.asyncio
async def test_colon_opens_the_command_line() -> None:
    app = _build_app(Dashboard(name='demo', widgets=[_metric('one')]))
    async with app.run_test(size=(160, 48)) as pilot:
        await _settle(app, pilot)
        await pilot.press('colon')
        await _wait_until(pilot, lambda: app.screen.query_one(CommandLine).display is True)


@pytest.mark.asyncio
async def test_which_key_lists_the_dashboard_bindings() -> None:
    app = _build_app(Dashboard(name='demo', widgets=[_metric('one')]))
    async with app.run_test(size=(160, 48)) as pilot:
        await _settle(app, pilot)
        await pilot.press('question_mark')
        await _wait_until(pilot, lambda: isinstance(app.screen, WhichKeyScreen))
        body = str(app.screen.query_one(Static).render())
        assert 'Dive' in body
        assert ':panels' in body


@pytest.mark.asyncio
async def test_load_failure_reports_in_the_status_line() -> None:
    async def load(_name: str) -> Dashboard:
        msg = 'access denied'
        raise RuntimeError(msg)

    app = _build_app(Dashboard(name='demo'), services=ShellServices(load_dashboard=load))
    async with app.run_test(size=(160, 48)) as pilot:
        await _settle(app, pilot)
        assert 'load failed: access denied' in str(_screen(app).query_one('#dash_status').render())


@pytest.mark.asyncio
async def test_every_widget_kind_renders_a_cell_and_stages() -> None:
    dashboard = Dashboard(
        name='demo',
        widgets=[
            LogWidget(layout=WidgetLayout(), title='api logs', query="SOURCE '/aws/lambda/api'"),
            TextWidget(layout=WidgetLayout(), markdown='# notes\n\nbody'),
            AlarmWidget(layout=WidgetLayout(), title='alarms', alarms=['arn:aws:cloudwatch:::alarm:high-errors']),
            UnknownWidget(layout=WidgetLayout(), widget_type='explorer'),
        ],
    )
    app = _build_app(dashboard)
    async with app.run_test(size=(160, 48)) as pilot:
        await _settle(app, pilot)
        screen = _screen(app)
        assert screen._staged == []  # no metric widget to stage
        staged: list[str] = []
        for index in range(4):
            screen._panels[index].cell.focus()
            await pilot.press('enter')
            await _wait_until(pilot, functools.partial(_is_staged, screen, index))
            inner = screen.query_one('#stage').children[0].children[0]
            staged.append(type(inner).__name__)
            if isinstance(inner, Static):
                staged.append(str(inner.render()))
        assert 'Markdown' in staged
        assert any('high-errors' in entry for entry in staged)
        assert any('Nothing to expand' in entry for entry in staged)


@pytest.mark.asyncio
async def test_metric_math_widget_captions_without_a_metric_stat() -> None:
    widget = MetricWidget(
        layout=WidgetLayout(),
        title='error rate',
        view='timeSeries',
        metrics=[[{'expression': 'errors / invocations', 'id': 'e'}]],
        period=300,
    )
    app = _build_app(Dashboard(name='demo', widgets=[widget]))
    async with app.run_test(size=(160, 48)) as pilot:
        await _settle(app, pilot)
        screen = _screen(app)
        assert 'metric math' in screen._metric_caption(screen._panels[0])
