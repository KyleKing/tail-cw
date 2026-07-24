"""Tests for the dashboard app: dive resolution, grid sizing, and driving."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tail_cw.aws.dashboards import (
    Dashboard,
    LogWidget,
    MetricWidget,
    TextWidget,
    WidgetLayout,
)
from tail_cw.aws.metrics import MetricSeries
from tail_cw.cli import DashboardRequest
from tail_cw.config import load_config
from tail_cw.tui.dashboard_app import DashboardApp, _grid_dimensions, resolve_log_group_for_widget


def _metric(title: str, *, view: str = 'timeSeries') -> MetricWidget:
    return MetricWidget(
        layout=WidgetLayout(width=12, height=6),
        title=title,
        view=view,
        metrics=[['AWS/Lambda', 'Invocations', 'FunctionName', 'my-fn', {'id': 'm', 'stat': 'Sum'}]],
        period=300,
    )


def test_resolve_log_group_from_log_widget_source() -> None:
    widget = LogWidget(layout=WidgetLayout(), title='t', query="SOURCE 'irm-ecs-api-prod' | fields @message")
    assert resolve_log_group_for_widget(widget) == 'irm-ecs-api-prod'


def test_resolve_log_group_from_metric_function_name() -> None:
    assert resolve_log_group_for_widget(_metric('m')) == '/aws/lambda/my-fn'


def test_resolve_log_group_none_for_text_widget() -> None:
    assert resolve_log_group_for_widget(TextWidget(layout=WidgetLayout(), markdown='# hi')) is None


@pytest.mark.parametrize(('count', 'expected'), [(1, (1, 1)), (4, (3, 2)), (9, (3, 3)), (12, (4, 3))])
def test_grid_dimensions_fit(count: int, expected: tuple[int, int]) -> None:
    assert _grid_dimensions(count) == expected


def _request() -> DashboardRequest:
    now = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
    return DashboardRequest(name='demo', start_time=now - timedelta(hours=3), end_time=now, region='us-east-1')


def _series(start: datetime) -> list[MetricSeries]:
    timestamps = [start + timedelta(minutes=30 * i) for i in range(6)]
    return [MetricSeries(id='m', label='Invocations', timestamps=timestamps, values=[float(i) for i in range(6)])]


@pytest.mark.asyncio
async def test_app_creates_cells_and_fetches_metrics() -> None:
    dashboard = Dashboard(
        name='demo', widgets=[_metric('one'), _metric('two'), TextWidget(layout=WidgetLayout(), markdown='# hi')]
    )
    calls: list[tuple[datetime, datetime]] = []

    def fetch(_queries: object, start: datetime, end: datetime) -> list[MetricSeries]:
        calls.append((start, end))
        return _series(start)

    app = DashboardApp(dashboard, _request(), load_config(), fetch_metrics=fetch)
    async with app.run_test(size=(160, 48)) as pilot:
        await pilot.pause()
        assert len(app._panels) == 3
        assert len(calls) == 2  # only the two metric widgets fetch


@pytest.mark.asyncio
async def test_promote_and_reset_toggle_the_stage() -> None:
    dashboard = Dashboard(name='demo', widgets=[_metric('one'), _metric('two')])
    app = DashboardApp(dashboard, _request(), load_config(), fetch_metrics=lambda *_: _series(_request().start_time))
    async with app.run_test(size=(160, 48)) as pilot:
        await pilot.pause()
        await pilot.press('enter')
        await pilot.pause()
        assert len(app._focused) == 1
        await pilot.press('escape')
        await pilot.pause()
        assert app._focused == []


@pytest.mark.asyncio
async def test_pan_shifts_window_and_refetches() -> None:
    dashboard = Dashboard(name='demo', widgets=[_metric('one')])
    calls: list[tuple[datetime, datetime]] = []

    def fetch(_queries: object, start: datetime, end: datetime) -> list[MetricSeries]:
        calls.append((start, end))
        return _series(start)

    request = _request()
    app = DashboardApp(dashboard, request, load_config(), fetch_metrics=fetch)
    async with app.run_test(size=(160, 48)) as pilot:
        await pilot.pause()
        await pilot.press('l')
        await pilot.pause()
        assert calls[-1][0] > request.start_time
