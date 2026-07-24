"""Compact cell rendering with Rich block characters (no image protocol).

Overview cells never use the terminal graphics protocol, so they are cheap,
crisp, and never ghost. A multi-series metric is reduced to a small, readable
set for the compact view (a min-max band with a median line by default, or a
chosen percentile, or a fixed pair) while the focused chart still draws every
series. Bars, single values, and lines each compact to a shape that keeps their
character.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from rich.console import Group, RenderableType
from rich.text import Text

from tail_cw.aws.metrics import MetricSeries
from tail_cw.charts.palette import role_color, series_color

_BLOCKS = '▁▂▃▄▅▆▇█'
_BAR_BLOCKS = '▁▂▃▄▅▆▇█'


class ReduceMode(StrEnum):
    """How a multi-series metric collapses in the compact view."""

    BAND = 'band'
    PERCENTILES = 'percentiles'
    SINGLE = 'single'
    EACH = 'each'


@dataclass(frozen=True)
class SparkRow:
    """One labeled sparkline row in a compact cell."""

    label: str
    values: list[float]
    color: str


def _resample(values: list[float], width: int) -> list[float]:
    if width <= 0 or not values:
        return []
    if len(values) <= width:
        return values
    step = len(values) / width
    return [values[min(len(values) - 1, int(i * step))] for i in range(width)]


def _blocks_for(values: list[float], charset: str, *, lo: float, hi: float) -> str:
    span = hi - lo
    if span <= 0:
        return charset[0] * len(values)
    last = len(charset) - 1
    return ''.join(charset[max(0, min(last, round((value - lo) / span * last)))] for value in values)


def sparkline_text(values: list[float], *, color: str, width: int, bars: bool = False) -> Text:
    """Render values as a single-line block sparkline in the given color."""
    resampled = _resample(values, width)
    if not resampled:
        return Text('', style=color)
    charset = _BAR_BLOCKS if bars else _BLOCKS
    lo, hi = min(resampled), max(resampled)
    return Text(_blocks_for(resampled, charset, lo=lo, hi=hi), style=color)


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
    if len(series) <= 2 or mode == ReduceMode.EACH:  # noqa: PLR2004
        return [SparkRow(item.label, item.values, series_color(index)) for index, item in enumerate(series)]
    if mode == ReduceMode.SINGLE:
        return [SparkRow(f'p{percentile:g}', _percentile_series(series, percentile), accent)]
    if mode == ReduceMode.PERCENTILES:
        return [
            SparkRow('p50', _percentile_series(series, 50.0), accent),
            SparkRow('p99', _percentile_series(series, 99.0), accent),
        ]
    return [
        SparkRow('median', _percentile_series(series, 50.0), accent),
        SparkRow(f'spread ({len(series)})', _percentile_series(series, 100.0), f'{accent} dim'),
    ]


def build_compact(
    title: str,
    view: str,
    series: list[MetricSeries],
    *,
    width: int,
    reduce_mode: ReduceMode = ReduceMode.BAND,
    percentile: float = 50.0,
) -> RenderableType:
    """Build the Rich renderable for a compact overview cell."""
    accent = role_color(title)
    if not series or not any(item.values for item in series):
        return Group(Text(title or '(untitled)', style=f'bold {accent}'), Text('no data', style='dim'))

    latest = next((item.values[-1] for item in series if item.values), 0.0)
    header = Text.assemble(
        (title or '(untitled)', f'bold {accent}'),
        ('  ', ''),
        (f'{latest:,.4g}', 'bold'),
    )

    if view == 'singleValue':
        trend = sparkline_text(series[0].values, color=accent, width=width)
        return Group(header, trend)

    bars = view == 'bar'
    rows = reduce_rows(series, accent=accent, mode=reduce_mode, percentile=percentile)
    show_labels = len(rows) > 1
    lines: list[RenderableType] = [header]
    for row in rows:
        spark_width = max(1, width - 6) if show_labels else max(1, width)
        spark = sparkline_text(row.values, color=row.color, width=spark_width, bars=bars)
        prefix = Text(f'{row.label[:5]:>5} ', style='dim') if show_labels else Text('')
        lines.append(Text.assemble(prefix, spark))
    return Group(*lines)
