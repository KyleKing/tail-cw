"""Tests for the native plotext chart widget."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from textual.app import App, ComposeResult

from tail_cw.aws.metrics import MetricSeries
from tail_cw.charts import ChartKind
from tail_cw.tui.plot_widget import PlotChart, _rgb, _time_ticks


def test_rgb_parses_hex() -> None:
    assert _rgb('#5aa9e6') == (90, 169, 230)


def test_time_ticks_are_floats_with_labels() -> None:
    base = datetime(2026, 7, 24, 11, tzinfo=UTC)
    timestamps = [base + timedelta(minutes=5 * i) for i in range(20)]
    positions, labels = _time_ticks(timestamps)
    assert positions
    assert all(isinstance(position, float) for position in positions)
    assert len(positions) == len(labels)
    assert labels[0] == '11:00'


def _series() -> list[MetricSeries]:
    base = datetime(2026, 7, 24, 11, tzinfo=UTC)
    timestamps = [base + timedelta(minutes=5 * i) for i in range(12)]
    return [
        MetricSeries(id='p50', label='p50', timestamps=timestamps, values=[float(i) for i in range(12)]),
        MetricSeries(id='p99', label='p99', timestamps=timestamps, values=[float(i * 3) for i in range(12)]),
    ]


class _Harness(App[None]):
    def __init__(self) -> None:
        super().__init__()
        self.chart = PlotChart(title='Latency (ms)', kind=ChartKind.LINE, colors=['#f6ae2d', '#e5484d'])

    def compose(self) -> ComposeResult:
        yield self.chart


@pytest.mark.asyncio
async def test_plot_renders_title_and_legend_natively() -> None:
    app = _Harness()
    async with app.run_test(size=(100, 30)) as pilot:
        app.chart.set_series(_series())
        await pilot.pause()
        svg = app.export_screenshot()
        assert 'Latency' in svg
        assert 'p99' in svg


@pytest.mark.asyncio
async def test_plot_message_when_no_data() -> None:
    app = _Harness()
    async with app.run_test(size=(100, 30)) as pilot:
        app.chart.set_message('No data in window')
        await pilot.pause()
        svg = app.export_screenshot()
        assert 'window' in svg
