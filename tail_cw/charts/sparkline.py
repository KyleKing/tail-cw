"""Compact cell rendering with Rich block characters (no image protocol).

Overview cells never use the terminal graphics protocol, so they are cheap,
crisp, and never ghost. A multi-series metric is reduced to a small, readable
set for the compact view (a min-max band with a median line by default, or a
chosen percentile, or a fixed pair) while the focused chart still draws every
series. Bars, single values, and lines each compact to a shape that keeps their
character.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from statistics import fmean

from rich.console import Group, RenderableType
from rich.text import Text

from tail_cw.aws.metrics import MetricSeries
from tail_cw.charts.palette import MetricRole, role_color, role_for, series_color
from tail_cw.text import shorten

_BLOCKS = '▁▂▃▄▅▆▇█'
_BAR_BLOCKS = '▁▂▃▄▅▆▇█'

Aggregate = Callable[[Sequence[float]], float]
"""How a column's source values collapse to one: `max` for counts and errors, `fmean` for gauges."""


class ReduceMode(StrEnum):
    """How a multi-series metric collapses in the compact view."""

    BAND = 'band'
    PERCENTILES = 'percentiles'
    SINGLE = 'single'
    EACH = 'each'


_LABEL_WIDTH = 5
"""Cells for a row label in a compact cell, the rest going to the sparkline.

`median` clipped to `media` reads as a different word rather than a shortened one,
so anything longer is cut with an ellipsis and our own labels are chosen to fit.
"""


@dataclass(frozen=True)
class SparkRow:
    """One labeled sparkline row in a compact cell."""

    label: str
    values: list[float]
    color: str


def _resample(values: list[float], width: int, aggregate: Aggregate) -> list[float]:
    """Downsample by aggregating each column's slice of source values, not by sampling one.

    Column boundaries are rounded independently, so every source value falls in exactly
    one column and none are dropped.
    """
    if width <= 0 or not values:
        return []
    if len(values) <= width:
        return list(values)
    step = len(values) / width
    columns = []
    for index in range(width):
        start = round(index * step)
        end = max(round((index + 1) * step), start + 1)
        columns.append(values[start:end] or [values[min(start, len(values) - 1)]])
    return [aggregate(column) for column in columns]


def _blocks_for(values: list[float], charset: str, *, lo: float, hi: float) -> str:
    span = hi - lo
    if span <= 0:
        return charset[0] * len(values)
    last = len(charset) - 1
    return ''.join(charset[max(0, min(last, round((value - lo) / span * last)))] for value in values)


def sparkline_blocks(
    values: list[float],
    *,
    width: int,
    bars: bool = False,
    lo: float | None = None,
    hi: float | None = None,
    aggregate: Aggregate = max,
) -> str:
    """Render values as a bare block sparkline.

    The scale spans the source data unless `lo` or `hi` pins it, so a downsampled cell
    still shows the real extremes. Counts usually want ``lo=0``, so a flat non-zero series
    does not render as the empty baseline.
    """
    if width <= 0 or not values:
        return ''
    low = min(values) if lo is None else lo
    high = max(values) if hi is None else hi
    resampled = _resample(values, width, aggregate)
    charset = _BAR_BLOCKS if bars else _BLOCKS
    return _blocks_for(resampled, charset, lo=low, hi=high)


def sparkline_text(
    values: list[float],
    *,
    color: str,
    width: int,
    bars: bool = False,
    aggregate: Aggregate = max,
) -> Text:
    """Render values as a single-line block sparkline in the given color."""
    return Text(sparkline_blocks(values, width=width, bars=bars, aggregate=aggregate), style=color)


def _percentile(sorted_values: list[float], percentile: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = percentile / 100 * (len(sorted_values) - 1)
    low = int(rank)
    high = min(low + 1, len(sorted_values) - 1)
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * (rank - low)


def _columns(series: list[MetricSeries]) -> list[list[float]]:
    length = min(len(item.values) for item in series)
    return [sorted(item.values[index] for item in series) for index in range(length)]


def _percentile_series(series: list[MetricSeries], percentile: float) -> list[float]:
    return [_percentile(column, percentile) for column in _columns(series)]


def reduce_rows(
    series: list[MetricSeries],
    *,
    accent: str,
    mode: ReduceMode,
    percentile: float,
) -> list[SparkRow]:
    """Reduce visible series to the small set of rows shown in a compact cell."""
    if not series:
        return []
    if len(series) <= 2 or mode == ReduceMode.EACH:  # ruff: ignore[magic-value-comparison]
        return [SparkRow(item.label, item.values, series_color(index)) for index, item in enumerate(series)]
    if mode == ReduceMode.SINGLE:
        return [SparkRow(f'p{percentile:g}', _percentile_series(series, percentile), accent)]
    if mode == ReduceMode.PERCENTILES:
        return [
            SparkRow('p50', _percentile_series(series, 50.0), accent),
            SparkRow('p99', _percentile_series(series, 99.0), accent),
        ]
    return [
        SparkRow('p50', _percentile_series(series, 50.0), accent),
        SparkRow('peak', _percentile_series(series, 100.0), f'{accent} dim'),
    ]


def build_compact(
    title: str,
    view: str,
    series: list[MetricSeries],
    *,
    width: int,
    reduce_mode: ReduceMode = ReduceMode.BAND,
    percentile: float = 50.0,
    theme_colors: Mapping[str, str] | None = None,
) -> RenderableType:
    """Build the Rich renderable for a compact overview cell."""
    accent = role_color(title, theme_colors=theme_colors)
    role = role_for(title)
    gauge_roles = {MetricRole.LATENCY, MetricRole.SATURATION, MetricRole.AVAILABILITY}
    aggregate: Aggregate = fmean if role in gauge_roles else max
    if not series or not any(item.values for item in series):
        return Group(Text(title or '(untitled)', style=f'bold {accent}'), Text('no data', style='dim'))

    latest = next((item.values[-1] for item in series if item.values), 0.0)
    header = Text.assemble(
        (title or '(untitled)', f'bold {accent}'),
        ('  ', ''),
        (f'{latest:,.4g}', 'bold'),
    )

    if view == 'singleValue':
        trend = sparkline_text(series[0].values, color=accent, width=width, aggregate=aggregate)
        return Group(header, trend)

    bars = view == 'bar'
    rows = reduce_rows(series, accent=accent, mode=reduce_mode, percentile=percentile)
    show_labels = len(rows) > 1
    lines: list[RenderableType] = [header]
    for row in rows:
        spark_width = max(1, width - 6) if show_labels else max(1, width)
        spark = sparkline_text(row.values, color=row.color, width=spark_width, bars=bars, aggregate=aggregate)
        prefix = Text(f'{shorten(row.label, _LABEL_WIDTH):>{_LABEL_WIDTH}} ', style='dim') if show_labels else Text('')
        lines.append(Text.assemble(prefix, spark))
    return Group(*lines)
