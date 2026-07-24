"""A Textual widget that renders a metric chart as an inline image.

Measures its allocated cell area, converts to pixels via the terminal's cell
size, and renders a matplotlib PNG at that exact size in a thread worker so the
chart fills its panel and stays sharp while the UI loop stays responsive. On a
terminal without a graphics protocol, ``textual-image`` falls back to Unicode
half-blocks automatically.
"""

from __future__ import annotations

import io

from textual.widget import Widget
from textual.widgets import Static
from textual_image.widget import Image, get_cell_size  # type: ignore[attr-defined]

from tail_cw.aws.metrics import MetricSeries
from tail_cw.charts.render import ChartKind, render_timeseries_png

_FALLBACK_CELL_PX = (10, 20)


class ChartWidget(Widget):
    """Renders one metric widget's series as a size-filling chart image."""

    DEFAULT_CSS = """
    ChartWidget {
        width: 1fr;
        height: 1fr;
    }
    ChartWidget > Image {
        width: 1fr;
        height: 1fr;
    }
    ChartWidget > #chart_placeholder {
        width: 1fr;
        height: 1fr;
        content-align: center middle;
        color: $text-muted;
    }
    """

    def __init__(
        self,
        *,
        title: str,
        kind: ChartKind = ChartKind.LINE,
        dark: bool = True,
    ) -> None:
        """Store the chart title, kind, and theme; series arrive via set_series."""
        super().__init__()
        self._title = title
        self._kind = kind
        self._dark = dark
        self._series: list[MetricSeries] = []
        self._image = Image()
        self._placeholder = Static('Loading...', id='chart_placeholder')
        self._last_size_px: tuple[int, int] = (0, 0)
        self._loaded = False

    def compose(self):  # noqa: ANN201, D102
        yield self._placeholder
        yield self._image

    def on_mount(self) -> None:
        """Hide the image until the first render completes."""
        self._image.display = False

    def set_series(self, series: list[MetricSeries], *, kind: ChartKind | None = None) -> None:
        """Replace the plotted series and re-render at the current size."""
        self._series = series
        if kind is not None:
            self._kind = kind
        self._loaded = True
        self._last_size_px = (0, 0)
        self._rerender()

    def set_message(self, message: str) -> None:
        """Show a text message instead of a chart (no data, error, or estimate prompt)."""
        self._loaded = False
        self._image.display = False
        self._placeholder.display = True
        self._placeholder.update(message)

    def on_resize(self) -> None:
        """Re-render at the new size when data is already loaded."""
        if self._loaded:
            self._rerender()

    def _target_pixels(self) -> tuple[int, int]:
        try:
            cell = get_cell_size()
            cell_w, cell_h = cell.width, cell.height
        except Exception:
            cell_w, cell_h = _FALLBACK_CELL_PX
        return self.content_size.width * cell_w, self.content_size.height * cell_h

    def _rerender(self) -> None:
        width_px, height_px = self._target_pixels()
        if width_px <= 0 or height_px <= 0:
            return
        if (width_px, height_px) == self._last_size_px:
            return
        self._last_size_px = (width_px, height_px)
        if not self._series:
            self.set_message('No data')
            return
        self.run_worker(
            lambda: self._render_png(width_px, height_px),
            name='chart_render',
            group=f'chart_render_{id(self)}',
            exclusive=True,
            thread=True,
        )

    def _render_png(self, width_px: int, height_px: int) -> None:
        png = render_timeseries_png(
            self._series,
            title=self._title,
            width_px=width_px,
            height_px=height_px,
            kind=self._kind,
            dark=self._dark,
        )
        self.app.call_from_thread(self._apply_png, png)

    def _apply_png(self, png: bytes) -> None:
        self._image.image = io.BytesIO(png)
        self._placeholder.display = False
        self._image.display = True
