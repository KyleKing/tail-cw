"""Tests for matplotlib PNG rendering of metric series."""

from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta

import pytest
from PIL import Image

from tail_cw.aws.metrics import MetricSeries
from tail_cw.charts.render import ChartKind, render_timeseries_png

_PNG_MAGIC = b'\x89PNG\r\n\x1a\n'


def _series(count: int = 12) -> list[MetricSeries]:
    base = datetime(2026, 7, 24, tzinfo=UTC)
    timestamps = [base + timedelta(minutes=5 * i) for i in range(count)]
    return [MetricSeries(id='a', label='reqs', timestamps=timestamps, values=[float(i % 5) for i in range(count)])]


@pytest.mark.parametrize('kind', [ChartKind.LINE, ChartKind.BAR])
def test_renders_valid_png(kind: ChartKind) -> None:
    png = render_timeseries_png(_series(), title='t', width_cells=80, height_cells=24, kind=kind)
    assert png.startswith(_PNG_MAGIC)


def test_dimensions_match_cell_grid() -> None:
    png = render_timeseries_png(_series(), title='t', width_cells=80, height_cells=24, cell_px=(8, 16))
    with Image.open(io.BytesIO(png)) as image:
        assert image.size == (640, 384)


def test_empty_series_still_renders() -> None:
    png = render_timeseries_png([], title='empty', width_cells=40, height_cells=10)
    assert png.startswith(_PNG_MAGIC)


def test_multiple_series_render_with_legend() -> None:
    series = [*_series(), MetricSeries(id='b', label='errs', timestamps=_series()[0].timestamps, values=[1.0] * 12)]
    png = render_timeseries_png(series, title='two', width_cells=80, height_cells=24)
    assert png.startswith(_PNG_MAGIC)
