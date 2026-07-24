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


_MIN_WIDTH_PX = 240
_MIN_HEIGHT_PX = 120
_BASE_FONT_PX = 13.0
_FONT_REFERENCE_HEIGHT_PX = 360.0


def render_timeseries_png(
    series: list[MetricSeries],
    *,
    title: str,
    width_px: int,
    height_px: int,
    kind: ChartKind = ChartKind.LINE,
    dark: bool = True,
    colors: list[str] | None = None,
) -> bytes:
    """Render metric series to PNG bytes at an exact pixel size.

    Font sizes scale with the panel height so text stays legible whether the
    panel is a small tile or a full-width chart.

    Args:
        series: One or more aligned series to draw.
        title: Chart title drawn at the top.
        width_px: Target width in pixels (the widget's allocated pixel area).
        height_px: Target height in pixels.
        kind: Line or bar rendering.
        dark: Use the dark palette when True, else the light palette.
        colors: Optional per-series colors; falls back to the categorical palette.

    Returns:
        PNG image bytes.
    """
    fg, bg, grid = (_DARK_FG, _DARK_BG, _DARK_GRID) if dark else (_LIGHT_FG, _LIGHT_BG, _LIGHT_GRID)
    width = max(_MIN_WIDTH_PX, width_px)
    height = max(_MIN_HEIGHT_PX, height_px)
    font_px = _BASE_FONT_PX * min(1.6, max(0.85, height / _FONT_REFERENCE_HEIGHT_PX))

    fig = Figure(figsize=(width / _DPI, height / _DPI), dpi=_DPI, facecolor=bg)
    try:
        ax = fig.add_subplot(111)
        ax.set_facecolor(bg)
        _draw_series(ax, series, kind=kind, colors=colors)
        _style_axes(ax, title=title, fg=fg, grid=grid, series_count=len(series), font_px=font_px)
        fig.tight_layout(pad=0.5)
        return _figure_to_png(fig)
    finally:
        plt.close(fig)


def _draw_series(ax: Axes, series: list[MetricSeries], *, kind: ChartKind, colors: list[str] | None = None) -> None:
    palette = colors or list(_SERIES_COLORS)
    for index, item in enumerate(series):
        color = palette[index % len(palette)]
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
    font_px: float,
) -> None:
    ax.set_title(title, color=fg, fontsize=font_px + 1, loc='left', pad=6)
    ax.tick_params(colors=fg, labelsize=font_px - 2)
    for spine in ax.spines.values():
        spine.set_color(grid)
    ax.grid(visible=True, color=grid, linewidth=0.5, alpha=0.6)
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    if series_count > 1:
        legend = ax.legend(loc='upper left', fontsize=font_px - 2, framealpha=0.0, ncols=min(series_count, 3))
        for text in legend.get_texts():
            text.set_color(fg)


def _figure_to_png(fig: Figure) -> bytes:
    buffer = io.BytesIO()
    fig.savefig(buffer, format='png', facecolor=fig.get_facecolor())
    return buffer.getvalue()
