"""Render metric series to PNG bytes with matplotlib.

Uses the Agg backend so rendering never needs a display server. The output is
raw PNG bytes suitable for a terminal graphics protocol widget. Rendering is a
pure function of the series and style inputs, so results can be cached by a
hash of those inputs.
"""

from __future__ import annotations

import io
from datetime import datetime
from enum import StrEnum

import matplotlib as mpl

mpl.use('Agg')

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.axes import Axes
from matplotlib.figure import Figure

from tail_cw.aws.metrics import MetricSeries

_DPI = 96
_DARK_FG = '#c8d0e0'
_DARK_BG = '#0f1117'
_DARK_GRID = '#2a2f3a'
_LIGHT_FG = '#1c1e26'
_LIGHT_BG = '#ffffff'
_LIGHT_GRID = '#d0d4dc'
_SERIES_COLORS = (
    '#5aa9e6',
    '#f6ae2d',
    '#f26419',
    '#8ac926',
    '#c77dff',
    '#ff5d8f',
    '#4ecdc4',
    '#ffd166',
)


class ChartKind(StrEnum):
    """Supported chart shapes mapped from CloudWatch widget views."""

    LINE = 'line'
    BAR = 'bar'


def render_timeseries_png(
    series: list[MetricSeries],
    *,
    title: str,
    width_cells: int,
    height_cells: int,
    cell_px: tuple[int, int] = (8, 16),
    kind: ChartKind = ChartKind.LINE,
    dark: bool = True,
) -> bytes:
    """Render metric series to PNG bytes sized to a terminal cell grid.

    Args:
        series: One or more aligned series to draw.
        title: Chart title drawn at the top.
        width_cells: Target width in terminal cells.
        height_cells: Target height in terminal cells.
        cell_px: Pixel size of one terminal cell as (width, height).
        kind: Line or bar rendering.
        dark: Use the dark palette when True, else the light palette.

    Returns:
        PNG image bytes.
    """
    fg, bg, grid = (_DARK_FG, _DARK_BG, _DARK_GRID) if dark else (_LIGHT_FG, _LIGHT_BG, _LIGHT_GRID)
    cell_w, cell_h = cell_px
    fig_w_in = max(1.0, width_cells * cell_w / _DPI)
    fig_h_in = max(1.0, height_cells * cell_h / _DPI)

    fig = Figure(figsize=(fig_w_in, fig_h_in), dpi=_DPI, facecolor=bg)
    try:
        ax = fig.add_subplot(111)
        ax.set_facecolor(bg)
        _draw_series(ax, series, kind=kind)
        _style_axes(ax, title=title, fg=fg, grid=grid, series_count=len(series))
        fig.tight_layout(pad=0.6)
        return _figure_to_png(fig)
    finally:
        plt.close(fig)


def _draw_series(ax: Axes, series: list[MetricSeries], *, kind: ChartKind) -> None:
    for index, item in enumerate(series):
        color = _SERIES_COLORS[index % len(_SERIES_COLORS)]
        if not item.timestamps:
            continue
        if kind == ChartKind.BAR:
            ax.bar(item.timestamps, item.values, color=color, label=item.label, width=_bar_width(item.timestamps))  # type: ignore[arg-type]
        else:
            ax.plot(item.timestamps, item.values, color=color, label=item.label, linewidth=1.4)  # type: ignore[arg-type]


def _bar_width(timestamps: list[datetime]) -> float:
    if len(timestamps) < 2:  # noqa: PLR2004
        return 0.01
    span_days = (timestamps[-1] - timestamps[0]).total_seconds() / 86400.0
    return max(span_days / max(1, len(timestamps)) * 0.8, 1e-4)


def _style_axes(
    ax: Axes,
    *,
    title: str,
    fg: str,
    grid: str,
    series_count: int,
) -> None:
    ax.set_title(title, color=fg, fontsize=10, loc='left', pad=6)
    ax.tick_params(colors=fg, labelsize=7)
    for spine in ax.spines.values():
        spine.set_color(grid)
    ax.grid(visible=True, color=grid, linewidth=0.5, alpha=0.6)
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    if series_count > 1:
        legend = ax.legend(loc='upper left', fontsize=6, framealpha=0.0, ncols=min(series_count, 3))
        for text in legend.get_texts():
            text.set_color(fg)


def _figure_to_png(fig: Figure) -> bytes:
    buffer = io.BytesIO()
    fig.savefig(buffer, format='png', facecolor=fig.get_facecolor())
    return buffer.getvalue()
