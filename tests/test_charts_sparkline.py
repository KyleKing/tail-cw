"""Tests for compact sparkline rendering and cross-series reduction."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from rich.console import Console

from tail_cw.aws.metrics import MetricSeries
from tail_cw.charts.sparkline import ReduceMode, build_compact, reduce_rows, sparkline_text

_BLOCKS = set('▁▂▃▄▅▆▇█')


def _series(count: int, base: float = 0.0, *, label: str = 's') -> MetricSeries:
    start = datetime(2026, 7, 24, tzinfo=UTC)
    timestamps = [start + timedelta(minutes=5 * i) for i in range(count)]
    return MetricSeries(id=label, label=label, timestamps=timestamps, values=[float(base + i) for i in range(count)])


def test_sparkline_text_uses_block_chars_at_target_width() -> None:
    text = sparkline_text([float(i) for i in range(8)], color='#fff', width=8)
    assert len(text.plain) == 8
    assert set(text.plain) <= _BLOCKS


def test_sparkline_resamples_down_to_width() -> None:
    text = sparkline_text([float(i) for i in range(100)], color='#fff', width=10)
    assert len(text.plain) == 10


def test_reduce_each_when_two_or_fewer_series() -> None:
    rows = reduce_rows([_series(6), _series(6, 10)], accent='#fff', mode=ReduceMode.BAND, percentile=50.0)
    assert len(rows) == 2


def test_reduce_band_gives_median_and_spread() -> None:
    series = [_series(6, base) for base in (0, 10, 20, 30)]
    rows = reduce_rows(series, accent='#fff', mode=ReduceMode.BAND, percentile=50.0)
    assert [row.label for row in rows] == ['median', 'spread (4)']


def test_reduce_percentiles_gives_p50_and_p99() -> None:
    series = [_series(6, base) for base in (0, 10, 20, 30)]
    rows = reduce_rows(series, accent='#fff', mode=ReduceMode.PERCENTILES, percentile=50.0)
    assert [row.label for row in rows] == ['p50', 'p99']


def test_reduce_single_uses_custom_percentile() -> None:
    series = [_series(6, base) for base in (0, 10, 20, 30)]
    rows = reduce_rows(series, accent='#fff', mode=ReduceMode.SINGLE, percentile=75.0)
    assert len(rows) == 1
    assert rows[0].label == 'p75'


def test_build_compact_singlevalue_shows_latest() -> None:
    renderable = build_compact('Availability %', 'singleValue', [_series(5, 90)], width=20)
    console = Console(width=30)
    with console.capture() as capture:
        console.print(renderable)
    assert '94' in capture.get()  # 90 + 4 (last of 0..4 offset)


def test_build_compact_no_data_message() -> None:
    console = Console(width=30)
    with console.capture() as capture:
        console.print(build_compact('CPU', 'timeSeries', [], width=20))
    assert 'no data' in capture.get()
