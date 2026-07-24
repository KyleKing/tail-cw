"""Textual app that renders a CloudWatch dashboard in the terminal.

The dashboard never scrolls. Every widget shows as a compact, color-coded cell
in a fit-to-screen grid (cheap Rich sparklines, no image protocol, so nothing
ghosts). Focusing a metric promotes it to a full matplotlib chart on a stage
above the grid; a second focus gives a two-up; reset returns to the even grid.
Only the one or two focused charts use the terminal graphics protocol, which is
what keeps rendering clean. Diving from a chart or log widget opens the existing
log table for that window and log group.

The app depends only on injected callables for AWS access so it can be driven
headless in tests with no network.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal
from textual.widgets import Footer, Header, Label, Static

from tail_cw.aws.dashboards import (
    AlarmWidget,
    Dashboard,
    LogWidget,
    MetricWidget,
    TextWidget,
    UnknownWidget,
    Widget,
)
from tail_cw.aws.metrics import MetricSeries, build_metric_data_queries
from tail_cw.charts.palette import role_color, series_color
from tail_cw.charts.render import ChartKind
from tail_cw.charts.sparkline import ReduceMode, build_compact
from tail_cw.cli import DashboardRequest
from tail_cw.config import TailCWConfig
from tail_cw.tui.chart_widget import ChartWidget
from tail_cw.tui.log_results import LogResultsScreen

FetchMetrics = Callable[[Sequence[dict[str, object]], datetime, datetime], list[MetricSeries]]
ResolveLogs = Callable[[str, datetime, datetime], Path | None]

_DEFAULT_PERIOD = 300
_MAX_FOCUS = 2
_STAT_CYCLE = ('Average', 'Sum', 'Minimum', 'Maximum', 'p95')
_PERIOD_CYCLE = (60, 300, 900, 3600, 21600)
_SOURCE_RE = re.compile(r"SOURCE\s+'([^']+)'")


def resolve_log_group_for_widget(widget: Widget) -> str | None:
    """Best-effort log group to dive into for a widget, or None when unknown."""
    match widget:
        case LogWidget():
            found = _SOURCE_RE.search(widget.query)
            return found.group(1) if found else None
        case MetricWidget():
            for row in widget.metrics:
                for index, element in enumerate(row):
                    if element == 'FunctionName' and index + 1 < len(row):
                        return f'/aws/lambda/{row[index + 1]}'
            return None
        case _:
            return None


def _grid_dimensions(count: int) -> tuple[int, int]:
    if count <= 1:
        return 1, 1
    columns = 3 if count <= 9 else 4  # noqa: PLR2004
    return columns, math.ceil(count / columns)


class CompactPanel(Static):
    """A focusable overview cell rendering a widget compactly (no image)."""

    DEFAULT_CSS = """
    CompactPanel {
        border: round $panel-lighten-1;
        padding: 0 1;
        height: 100%;
        width: 100%;
        overflow: hidden;
        scrollbar-size: 0 0;
    }
    CompactPanel:focus {
        border: round $accent;
    }
    """

    def __init__(self, index: int, widget: Widget) -> None:
        """Create the cell for the widget at the given dashboard index."""
        super().__init__(id=f'cell-{index}')
        self.index = index
        self.widget = widget
        self.can_focus = True
        self._series: list[MetricSeries] = []
        self._reduce_mode = ReduceMode.BAND
        self._percentile = 50.0

    def set_series(self, series: list[MetricSeries]) -> None:
        """Store the metric series and render the compact sparkline."""
        self._series = series
        self._render_compact()

    def render_static(self, renderable: Any) -> None:
        """Render fixed content (used for log, text, and alarm cells)."""
        self.update(renderable)

    def on_resize(self) -> None:
        """Re-render the compact sparkline at the new width."""
        if isinstance(self.widget, MetricWidget) and self._series:
            self._render_compact()

    def _render_compact(self) -> None:
        if not isinstance(self.widget, MetricWidget):
            return
        width = self.content_size.width or 24
        self.update(
            build_compact(
                self.widget.title,
                self.widget.view,
                self._series,
                width=width,
                reduce_mode=self._reduce_mode,
                percentile=self._percentile,
            ),
        )


@dataclass
class _Panel:
    widget: Widget
    cell: CompactPanel
    series: list[MetricSeries] = field(default_factory=list)
    stat_override: str | None = None
    period_override: int | None = None


class DashboardApp(App[None]):
    """Renders one dashboard as a no-scroll grid with a focus-expand stage."""

    CSS = """
    #stage {
        height: 0;
        display: none;
    }
    #stage.active {
        height: 60%;
        display: block;
    }
    #grid {
        layout: grid;
        grid-gutter: 1;
        height: 1fr;
    }
    #dash_status {
        dock: bottom;
        height: 1;
        background: $panel;
        color: $text;
        padding: 0 1;
    }
    """

    BINDINGS: ClassVar[Sequence[Binding]] = [  # type: ignore[assignment]
        Binding('q', 'quit', 'Quit'),
        Binding('j', 'focus_next', 'Next'),
        Binding('k', 'focus_prev', 'Prev'),
        Binding('enter', 'promote', 'Focus'),
        Binding('space', 'add_focus', '+Focus'),
        Binding('escape', 'reset_focus', 'Reset'),
        Binding('s', 'cycle_stat', 'Stat'),
        Binding('p', 'cycle_period', 'Period'),
        Binding('h', 'pan_earlier', 'Pan <'),
        Binding('l', 'pan_later', 'Pan >'),
        Binding('d', 'dive', 'Dive'),
    ]

    def __init__(
        self,
        dashboard: Dashboard,
        request: DashboardRequest,
        config: TailCWConfig,
        *,
        fetch_metrics: FetchMetrics,
        resolve_logs: ResolveLogs | None = None,
    ) -> None:
        """Store the dashboard, its metric window, and the injected AWS callables."""
        super().__init__()
        self.title = f'Dashboard: {dashboard.name}'
        self._dashboard = dashboard
        self._config = config
        self._fetch_metrics = fetch_metrics
        self._resolve_logs = resolve_logs
        self._start = request.start_time
        self._end = request.end_time
        self._panels: list[_Panel] = []
        self._panel_by_cell_id: dict[int, _Panel] = {}
        self._focused: list[int] = []

    def compose(self) -> ComposeResult:
        """Build the header, the focus stage, the compact grid, and the status line.

        Yields:
            The header, the stage container, the grid container, a status label, and the footer.
        """
        yield Header(show_clock=True)
        yield Horizontal(id='stage')
        with Container(id='grid'):
            for index, widget in enumerate(self._dashboard.widgets):
                cell = CompactPanel(index, widget)
                panel = _Panel(widget=widget, cell=cell)
                self._panels.append(panel)
                self._panel_by_cell_id[id(cell)] = panel
                yield cell
        yield Label('', id='dash_status')
        yield Footer()

    def on_mount(self) -> None:
        """Size the grid to fit, render fixed cells, fetch metrics, focus the first cell."""
        columns, rows = _grid_dimensions(len(self._panels))
        grid = self.query_one('#grid')
        grid.styles.grid_size_columns = columns
        grid.styles.grid_size_rows = rows
        for panel in self._panels:
            self._render_fixed_cell(panel)
        self._update_status()
        self._load_all_metrics()
        if self._panels:
            self._panels[0].cell.focus()

    @staticmethod
    def _render_fixed_cell(panel: _Panel) -> None:
        widget = panel.widget
        match widget:
            case LogWidget():
                accent = role_color(widget.title or 'logs')
                panel.cell.render_static(f'[bold {accent}]{widget.title or "Logs"}[/]\n[dim]{widget.query[:120]}[/]')
            case TextWidget():
                panel.cell.render_static(widget.markdown)
            case AlarmWidget():
                names = '\n'.join(alarm.rsplit(':', 1)[-1] for alarm in widget.alarms) or '(no alarms)'
                panel.cell.render_static(f'[bold]{widget.title or "Alarms"}[/]\n{names}')
            case UnknownWidget():
                panel.cell.render_static(f'[dim]Unsupported widget: {widget.widget_type}[/]')
            case _:
                pass

    def _load_all_metrics(self) -> None:
        for panel in self._panels:
            if isinstance(panel.widget, MetricWidget):
                self._load_panel(panel)

    def _load_panel(self, panel: _Panel) -> None:
        self.run_worker(
            lambda: self._fetch_panel(panel),
            name='metric_fetch',
            group=f'metric_fetch_{panel.cell.index}',
            exclusive=True,
            thread=True,
        )

    def _fetch_panel(self, panel: _Panel) -> None:
        widget = panel.widget
        if not isinstance(widget, MetricWidget):
            return
        queries = build_metric_data_queries(
            widget.metrics,
            widget_stat=panel.stat_override or widget.stat,
            widget_period=panel.period_override or widget.period,
            default_period=_DEFAULT_PERIOD,
        )
        if not queries:
            return
        try:
            series = self._fetch_metrics(queries, self._start, self._end)
        except Exception as err:
            self.call_from_thread(panel.cell.render_static, f'[red]error:[/] {err}')
            return
        visible = [item for item in series if item.values]
        panel.series = visible
        self.call_from_thread(panel.cell.set_series, visible)
        if panel.cell.index in self._focused:
            self.call_from_thread(self._rebuild_stage)

    def _focused_panel(self) -> _Panel | None:
        return self._panel_by_cell_id.get(id(self.focused)) if self.focused is not None else None

    def action_focus_next(self) -> None:
        """Move focus to the next cell."""
        self.screen.focus_next()

    def action_focus_prev(self) -> None:
        """Move focus to the previous cell."""
        self.screen.focus_previous()

    def action_promote(self) -> None:
        """Focus the current metric cell as the only chart on the stage."""
        panel = self._focused_panel()
        if panel is None or not isinstance(panel.widget, MetricWidget):
            return
        self._focused = [panel.cell.index]
        self._rebuild_stage()

    def action_add_focus(self) -> None:
        """Add the current metric cell as a second chart on the stage."""
        panel = self._focused_panel()
        if panel is None or not isinstance(panel.widget, MetricWidget):
            return
        if panel.cell.index in self._focused:
            return
        self._focused.append(panel.cell.index)
        self._focused = self._focused[-_MAX_FOCUS:]
        self._rebuild_stage()

    def action_reset_focus(self) -> None:
        """Clear the stage and return to the even grid."""
        self._focused = []
        self._rebuild_stage()

    def _rebuild_stage(self) -> None:
        stage = self.query_one('#stage', Horizontal)
        for child in list(stage.children):
            child.remove()
        if not self._focused:
            stage.remove_class('active')
            self._update_status()
            return
        for index in self._focused:
            panel = self._panels[index]
            if not isinstance(panel.widget, MetricWidget):
                continue
            kind = ChartKind.BAR if panel.widget.view == 'bar' else ChartKind.LINE
            colors = [role_color(panel.widget.title)] if len(panel.series) == 1 else _series_colors(panel.series)
            chart = ChartWidget(title=panel.widget.title or '(untitled)', kind=kind, colors=colors)
            stage.mount(chart)
            chart.set_series(panel.series)
        stage.add_class('active')
        self._update_status()

    def action_cycle_stat(self) -> None:
        """Cycle the statistic on the focused metric cell and re-fetch it."""
        panel = self._focused_panel()
        if panel is None or not isinstance(panel.widget, MetricWidget):
            return
        current = panel.stat_override or panel.widget.stat or _STAT_CYCLE[0]
        index = (_STAT_CYCLE.index(current) + 1) % len(_STAT_CYCLE) if current in _STAT_CYCLE else 0
        panel.stat_override = _STAT_CYCLE[index]
        self._update_status(f'{panel.widget.title}: stat -> {panel.stat_override}')
        self._load_panel(panel)

    def action_cycle_period(self) -> None:
        """Cycle the period on the focused metric cell and re-fetch it."""
        panel = self._focused_panel()
        if panel is None or not isinstance(panel.widget, MetricWidget):
            return
        current = panel.period_override or panel.widget.period or _DEFAULT_PERIOD
        index = (_PERIOD_CYCLE.index(current) + 1) % len(_PERIOD_CYCLE) if current in _PERIOD_CYCLE else 1
        panel.period_override = _PERIOD_CYCLE[index]
        self._update_status(f'{panel.widget.title}: period -> {panel.period_override}s')
        self._load_panel(panel)

    def _shift_window(self, factor: float) -> None:
        delta = (self._end - self._start) * factor
        self._start += delta
        self._end += delta
        self._update_status()
        self._load_all_metrics()

    def action_pan_earlier(self) -> None:
        """Shift the window a quarter-span earlier."""
        self._shift_window(-0.25)

    def action_pan_later(self) -> None:
        """Shift the window a quarter-span later."""
        self._shift_window(0.25)

    def action_dive(self) -> None:
        """Open the log table for the focused widget's log group and window."""
        panel = self._focused_panel()
        if panel is None:
            return
        log_group = resolve_log_group_for_widget(panel.widget)
        if log_group is None:
            self.notify('No log source resolved for this widget', severity='warning')
            return
        if self._resolve_logs is None:
            self.notify(f'Would open logs for {log_group} (no log pipeline wired)', severity='information')
            return
        self.notify(f'Fetching logs for {log_group}...', severity='information')
        self.run_worker(
            lambda: self._dive_worker(log_group, self._start, self._end),
            name='dive_fetch',
            group='dive_fetch',
            exclusive=True,
            thread=True,
        )

    def _dive_worker(self, log_group: str, start: datetime, end: datetime) -> None:
        if self._resolve_logs is None:
            return
        try:
            parquet_path = self._resolve_logs(log_group, start, end)
        except Exception as err:
            self.call_from_thread(self.notify, f'Log fetch failed: {err}', severity='error')
            return
        if parquet_path is None:
            self.call_from_thread(self.notify, f'No log events for {log_group} in this window', severity='warning')
            return
        title = f'{log_group} · {start:%H:%M}-{end:%H:%M}'
        self.call_from_thread(self.push_screen, LogResultsScreen(parquet_path, self._config, title=title))

    def _update_status(self, message: str | None = None) -> None:
        window = f'{self._start:%Y-%m-%d %H:%M} -> {self._end:%H:%M} UTC'
        focus = f' · {len(self._focused)} focused' if self._focused else ''
        base = f'{len(self._panels)} panels · window {window}{focus}'
        self.query_one('#dash_status', Label).update(f'{base} · {message}' if message else base)


def _series_colors(series: list[MetricSeries]) -> list[str]:
    return [series_color(index) for index in range(len(series))]
