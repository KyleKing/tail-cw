"""Tests for the dashboard command line and command dispatch."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from textual.app import App, ComposeResult

from tail_cw.aws.dashboards import Dashboard, MetricWidget, WidgetLayout
from tail_cw.aws.metrics import MetricSeries
from tail_cw.cli import DashboardRequest
from tail_cw.config import load_config
from tail_cw.tui.command_bar import CommandLine
from tail_cw.tui.dashboard_app import DashboardApp, WhichKeyScreen


def _metric(title: str) -> MetricWidget:
    return MetricWidget(
        layout=WidgetLayout(width=12, height=6),
        title=title,
        view='timeSeries',
        metrics=[['AWS/Lambda', 'Invocations', {'id': 'm', 'stat': 'Sum'}]],
        period=300,
    )


def _request() -> DashboardRequest:
    now = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
    return DashboardRequest(name='demo', start_time=now - timedelta(hours=3), end_time=now, region='us-east-1')


def _series(start: datetime) -> list[MetricSeries]:
    timestamps = [start + timedelta(minutes=30 * i) for i in range(6)]
    return [MetricSeries(id='m', label='Invocations', timestamps=timestamps, values=[float(i) for i in range(6)])]


def _app() -> DashboardApp:
    dashboard = Dashboard(name='demo', widgets=[_metric('Latency (ms)'), _metric('Requests / 5 min')])
    return DashboardApp(dashboard, _request(), load_config(), fetch_metrics=lambda *_: _series(_request().start_time))


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


@pytest.mark.asyncio
async def test_completion_covers_names_args_and_panels() -> None:
    app = _app()
    async with app.run_test(size=(160, 48)) as pilot:
        await pilot.pause()
        assert 'range' in app._complete_command('ra')
        assert 'stat Average' in app._complete_command('stat ')
        assert 'focus Latency (ms)' in app._complete_command('focus Lat')


@pytest.mark.asyncio
async def test_colon_opens_command_line() -> None:
    app = _app()
    async with app.run_test(size=(160, 48)) as pilot:
        await pilot.pause()
        await pilot.press('colon')
        await pilot.pause()
        assert app.query_one(CommandLine).display is True


@pytest.mark.asyncio
async def test_range_command_sets_window() -> None:
    app = _app()
    async with app.run_test(size=(160, 48)) as pilot:
        await pilot.pause()
        app._run_command('range 1h')
        await pilot.pause()
        assert app._end - app._start == timedelta(hours=1)


@pytest.mark.asyncio
async def test_focus_command_matches_by_title() -> None:
    app = _app()
    async with app.run_test(size=(160, 48)) as pilot:
        await pilot.pause()
        app._run_command('focus Requests')
        await pilot.pause()
        assert [app._panels[i].widget.title for i in app._focused] == ['Requests / 5 min']  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_filter_hides_non_matching_and_resets() -> None:
    app = _app()
    async with app.run_test(size=(160, 48)) as pilot:
        await pilot.pause()
        app._run_command('filter latency')
        await pilot.pause()
        visible = [p.widget.title for p in app._panels if p.cell.index not in app._hidden]  # type: ignore[union-attr]
        assert visible == ['Latency (ms)']
        app._run_command('filter all')
        await pilot.pause()
        assert app._hidden == set()


@pytest.mark.asyncio
async def test_leader_opens_which_key() -> None:
    app = _app()
    async with app.run_test(size=(160, 48)) as pilot:
        await pilot.pause()
        await pilot.press('comma')
        await pilot.pause()
        assert isinstance(app.screen, WhichKeyScreen)
