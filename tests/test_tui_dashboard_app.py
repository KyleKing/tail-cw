"""Tests for the dashboard app: layout helpers, dive resolution, and driving."""

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
from tail_cw.tui.dashboard_app import DashboardApp, _widget_rows, resolve_log_group_for_widget


def _metric(title: str, *, y: int = 0, x: int = 0, view: str = 'timeSeries') -> MetricWidget:
    return MetricWidget(
        layout=WidgetLayout(x=x, y=y, width=12, height=6),
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


def test_widget_rows_group_by_y_then_x() -> None:
    widgets = [_metric('b', y=0, x=12), _metric('a', y=0, x=0), _metric('c', y=6, x=0)]
    rows = _widget_rows(widgets)
    assert [[getattr(w, 'title', '') for w in row] for row in rows] == [['a', 'b'], ['c']]


def _request() -> DashboardRequest:
    now = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
    return DashboardRequest(name='demo', start_time=now - timedelta(hours=3), end_time=now, region='us-east-1')


def _series(start: datetime) -> list[MetricSeries]:
    timestamps = [start + timedelta(minutes=30 * i) for i in range(6)]
    return [MetricSeries(id='m', label='Invocations', timestamps=timestamps, values=[float(i) for i in range(6)])]


@pytest.mark.asyncio
async def test_app_creates_panels_and_fetches() -> None:
    dashboard = Dashboard(name='demo', widgets=[_metric('one'), _metric('two', x=12)])
    calls: list[tuple[datetime, datetime]] = []

    def fetch(_queries: object, start: datetime, end: datetime) -> list[MetricSeries]:
        calls.append((start, end))
        return _series(start)

    app = DashboardApp(dashboard, _request(), load_config(), fetch_metrics=fetch)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        assert len(app._panels) == 2
        assert len(calls) == 2


@pytest.mark.asyncio
async def test_pan_shifts_window_and_refetches() -> None:
    dashboard = Dashboard(name='demo', widgets=[_metric('one')])
    calls: list[tuple[datetime, datetime]] = []

    def fetch(_queries: object, start: datetime, end: datetime) -> list[MetricSeries]:
        calls.append((start, end))
        return _series(start)

    request = _request()
    app = DashboardApp(dashboard, request, load_config(), fetch_metrics=fetch)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        await pilot.press('l')
        await pilot.pause()
        assert calls[-1][0] > request.start_time


@pytest.mark.asyncio
async def test_dive_without_pipeline_notifies() -> None:
    dashboard = Dashboard(name='demo', widgets=[_metric('one')])
    app = DashboardApp(dashboard, _request(), load_config(), fetch_metrics=lambda *_: _series(_request().start_time))
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        await pilot.press('d')
        await pilot.pause()
