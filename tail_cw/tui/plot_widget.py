"""A focused-chart widget drawn with plotext (native cells, no image protocol).

plotext renders the chart as Unicode braille plus real text axes, ticks, and a
legend, all through Textual's normal compositor. That sidesteps the terminal
graphics protocol entirely, so there is no ghosting, no stray colored strips,
and no illegible downsampled text, and it resizes cleanly.
"""

from __future__ import annotations

from datetime import datetime

from textual_plotext import PlotextPlot

from tail_cw.aws.metrics import MetricSeries
from tail_cw.charts import ChartKind
from tail_cw.charts.palette import series_color

_TICKS = 5


def _rgb(hex_color: str) -> tuple[int, int, int]:
    text = hex_color.lstrip('#')
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)


def _time_ticks(timestamps: list[datetime]) -> tuple[list[float], list[str]]:
    if not timestamps:
        return [], []
    count = len(timestamps)
    step = max(1, count // _TICKS)
    positions = list(range(0, count, step))
    labels = [timestamps[index].strftime('%H:%M') for index in positions]
    return [float(position) for position in positions], labels


class PlotChart(PlotextPlot):
    """Renders one metric widget's series as a native plotext chart."""

    def __init__(
        self,
        *,
        title: str,
        kind: ChartKind = ChartKind.LINE,
        colors: list[str] | None = None,
    ) -> None:
        """Store the chart title, kind, and series colors; series arrive via set_series."""
        super().__init__()
        self._title = title
        self._kind = kind
        self._colors = colors
        self._series: list[MetricSeries] = []
        self._message: str | None = 'Loading...'

    def set_series(self, series: list[MetricSeries]) -> None:
        """Replace the plotted series and re-render."""
        self._series = series
        self._message = None
        self._replot()

    def set_message(self, message: str) -> None:
        """Show a status message in place of a chart."""
        self._message = message
        self._series = []
        self._replot()

    def _replot(self) -> None:
        plt = self.plt
        plt.clear_figure()
        plt.theme('dark')
        if not self._series:
            # plotext will not draw a title without any data, so plot a faint
            # baseline to carry the message.
            plt.plot([0.0, 1.0], [0.0, 0.0], color=(70, 70, 70))
            plt.title(f'{self._title} — {self._message or "no data"}')
            self.refresh()
            return
        for index, item in enumerate(self._series):
            color = _rgb(self._colors[index]) if self._colors else _rgb(series_color(index))
            xs = list(range(len(item.values)))
            if self._kind == ChartKind.BAR:
                plt.bar(xs, item.values, label=item.label, color=color)
            else:
                plt.plot(xs, item.values, label=item.label, color=color)
        positions, labels = _time_ticks(self._series[0].timestamps)
        if positions:
            plt.xticks(positions, labels)
        plt.title(self._title)
        self.refresh()
