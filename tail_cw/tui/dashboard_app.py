"""Textual app that renders a CloudWatch dashboard in the terminal.

Lays the dashboard's widgets into rows, fetches each metric widget's series via
an injected callable, and renders them as inline charts. The focused panel takes
keyboard inputs to change the shared time window, the statistic, and the period,
re-rendering only what changed. Diving from a chart or log widget opens the
existing log table filtered to that widget's window and log group.

The app depends only on injected callables for AWS access so it can be driven
headless in tests with no network.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import groupby
from pathlib import Path
from typing import ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Footer, Header, Label, Markdown, Static

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
from tail_cw.charts.render import ChartKind
from tail_cw.cli import DashboardRequest
from tail_cw.config import TailCWConfig
from tail_cw.tui.chart_widget import ChartWidget
from tail_cw.tui.log_results import LogResultsScreen

FetchMetrics = Callable[[Sequence[dict[str, object]], datetime, datetime], list[MetricSeries]]
ResolveLogs = Callable[[str, datetime, datetime], Path | None]

_DEFAULT_PERIOD = 300
_ROW_UNIT_CELLS = 3
_MIN_ROW_CELLS = 8
_STAT_CYCLE = ('Average', 'Sum', 'Minimum', 'Maximum', 'p95')
_SOURCE_RE = re.compile(r"SOURCE\s+'([^']+)'")


def _widget_rows(widgets: Sequence[Widget]) -> list[list[Widget]]:
    """Group widgets into visual rows by their y coordinate, ordered left to right."""
    ordered = sorted(widgets, key=lambda w: (w.layout.y, w.layout.x))
    return [sorted(group, key=lambda w: w.layout.x) for _, group in groupby(ordered, key=lambda w: w.layout.y)]


def _row_height_cells(row: Sequence[Widget]) -> int:
    return max(_MIN_ROW_CELLS, max(widget.layout.height for widget in row) * _ROW_UNIT_CELLS)


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


@dataclass
class _MetricPanel:
    widget: MetricWidget
    chart: ChartWidget
    stat_override: str | None = None
    period_override: int | None = None


class DashboardApp(App[None]):
    """Renders one dashboard and drives metric exploration and log dives."""

    CSS = """
    #dashboard_scroll {
        height: 1fr;
    }
    .dash_row {
        width: 100%;
    }
    .panel {
        border: round $panel-lighten-2;
        padding: 0 1;
    }
    .panel:focus-within, .panel:focus {
        border: round $accent;
    }
    .panel_text {
        border: round $panel-lighten-2;
        padding: 0 1;
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
        Binding('q', 'quit', 'Quit', show=True),
        Binding('j', 'focus_next', 'Next', show=True),
        Binding('k', 'focus_prev', 'Prev', show=True),
        Binding('r', 'refresh', 'Refresh', show=True),
        Binding('s', 'cycle_stat', 'Stat', show=True),
        Binding('p', 'cycle_period', 'Period', show=True),
        Binding('h', 'pan_earlier', 'Pan <', show=True),
        Binding('l', 'pan_later', 'Pan >', show=True),
        Binding('minus', 'zoom_out', 'Zoom out', show=False, key_display='-'),
        Binding('plus', 'zoom_in', 'Zoom in', show=False, key_display='+'),
        Binding('d', 'dive', 'Dive to logs', show=True),
        Binding('?', 'help', 'Help', show=False),
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
        self._panels: list[_MetricPanel] = []
        self._panel_by_widget_id: dict[int, _MetricPanel] = {}
        self._widget_by_focusable: dict[int, Widget] = {}

    def compose(self) -> ComposeResult:
        """Lay the dashboard's widgets into rows of panels.

        Yields:
            The header, one row container per widget row, a status label, and the footer.
        """
        yield Header(show_clock=True)
        with VerticalScroll(id='dashboard_scroll'):
            for row in _widget_rows(self._dashboard.widgets):
                height = _row_height_cells(row)
                with Horizontal(classes='dash_row') as container:
                    container.styles.height = height
                    for widget in row:
                        yield from self._compose_panel(widget)
        yield Label('', id='dash_status')
        yield Footer()

    def _compose_panel(self, widget: Widget) -> ComposeResult:
        span = max(1, widget.layout.width)
        match widget:
            case MetricWidget():
                kind = ChartKind.BAR if widget.view == 'bar' else ChartKind.LINE
                chart = ChartWidget(title=widget.title or '(untitled)', kind=kind)
                chart.styles.width = f'{span}fr'
                chart.add_class('panel')
                chart.can_focus = True
                panel = _MetricPanel(widget=widget, chart=chart)
                self._panels.append(panel)
                self._panel_by_widget_id[id(chart)] = panel
                self._widget_by_focusable[id(chart)] = widget
                yield chart
            case LogWidget():
                body = Static(f'[b]{widget.title or "Logs"}[/b]\n\n{widget.query}', classes='panel_text')
                body.styles.width = f'{span}fr'
                body.can_focus = True
                self._widget_by_focusable[id(body)] = widget
                yield body
            case TextWidget():
                markdown = Markdown(widget.markdown, classes='panel_text')
                markdown.styles.width = f'{span}fr'
                yield markdown
            case AlarmWidget():
                names = '\n'.join(alarm.rsplit(':', 1)[-1] for alarm in widget.alarms) or '(no alarms)'
                alarm_body = Static(f'[b]{widget.title or "Alarms"}[/b]\n\n{names}', classes='panel_text')
                alarm_body.styles.width = f'{span}fr'
                yield alarm_body
            case UnknownWidget():
                unknown_body = Static(f'[dim]Unsupported widget: {widget.widget_type}[/dim]', classes='panel_text')
                unknown_body.styles.width = f'{span}fr'
                yield unknown_body

    def on_mount(self) -> None:
        """Fetch every metric panel and focus the first chart."""
        self._update_status()
        self._load_all_metrics()
        if self._panels:
            self._panels[0].chart.focus()

    def _load_all_metrics(self) -> None:
        for panel in self._panels:
            self._load_panel(panel)

    def _load_panel(self, panel: _MetricPanel) -> None:
        panel.chart.set_message('Loading...')
        self.run_worker(
            lambda: self._fetch_panel(panel),
            name='metric_fetch',
            group=f'metric_fetch_{id(panel.chart)}',
            exclusive=True,
            thread=True,
        )

    def _fetch_panel(self, panel: _MetricPanel) -> None:
        widget = panel.widget
        queries = build_metric_data_queries(
            widget.metrics,
            widget_stat=panel.stat_override or widget.stat,
            widget_period=panel.period_override or widget.period,
            default_period=_DEFAULT_PERIOD,
        )
        if not queries:
            self.call_from_thread(panel.chart.set_message, 'No metrics defined')
            return
        try:
            series = self._fetch_metrics(queries, self._start, self._end)
        except Exception as err:
            self.call_from_thread(panel.chart.set_message, f'Error: {err}')
            return
        if not any(item.values for item in series):
            self.call_from_thread(panel.chart.set_message, 'No data in window')
            return
        self.call_from_thread(panel.chart.set_series, [item for item in series if item.values])

    def _focused_metric_panel(self) -> _MetricPanel | None:
        focused = self.focused
        return self._panel_by_widget_id.get(id(focused)) if focused is not None else None

    def action_focus_next(self) -> None:
        """Move focus to the next panel."""
        self.screen.focus_next()

    def action_focus_prev(self) -> None:
        """Move focus to the previous panel."""
        self.screen.focus_previous()

    def action_refresh(self) -> None:
        """Re-fetch every panel with the window's end extended to now."""
        span = self._end - self._start
        self._end = datetime.now(tz=UTC)
        self._start = self._end - span
        self._update_status()
        self._load_all_metrics()

    def action_cycle_stat(self) -> None:
        """Cycle the statistic on the focused chart and re-fetch it."""
        panel = self._focused_metric_panel()
        if panel is None:
            self.notify('Focus a metric chart to change its statistic', severity='information')
            return
        current = panel.stat_override or panel.widget.stat or _STAT_CYCLE[0]
        index = (_STAT_CYCLE.index(current) + 1) % len(_STAT_CYCLE) if current in _STAT_CYCLE else 0
        panel.stat_override = _STAT_CYCLE[index]
        self._update_status(f'{panel.widget.title}: stat -> {panel.stat_override}')
        self._load_panel(panel)

    def action_cycle_period(self) -> None:
        """Cycle the period on the focused chart and re-fetch it."""
        panel = self._focused_metric_panel()
        if panel is None:
            self.notify('Focus a metric chart to change its period', severity='information')
            return
        periods = (60, 300, 900, 3600, 21600)
        current = panel.period_override or panel.widget.period or _DEFAULT_PERIOD
        index = (periods.index(current) + 1) % len(periods) if current in periods else 1
        panel.period_override = periods[index]
        self._update_status(f'{panel.widget.title}: period -> {panel.period_override}s')
        self._load_panel(panel)

    def _shift_window(self, factor: float) -> None:
        delta = (self._end - self._start) * factor
        self._start += delta
        self._end += delta
        self._update_status()
        self._load_all_metrics()

    def _zoom_window(self, factor: float) -> None:
        span = self._end - self._start
        center = self._start + span / 2
        half = span * factor / 2
        self._start = center - half
        self._end = center + half
        self._update_status()
        self._load_all_metrics()

    def action_pan_earlier(self) -> None:
        """Shift the window a quarter-span earlier."""
        self._shift_window(-0.25)

    def action_pan_later(self) -> None:
        """Shift the window a quarter-span later."""
        self._shift_window(0.25)

    def action_zoom_out(self) -> None:
        """Double the window around its center."""
        self._zoom_window(2.0)

    def action_zoom_in(self) -> None:
        """Halve the window around its center."""
        self._zoom_window(0.5)

    def action_dive(self) -> None:
        """Open the log table for the focused widget's log group and window."""
        widget = self._focused_widget()
        if widget is None:
            self.notify('Focus a chart or log widget to dive', severity='information')
            return
        log_group = resolve_log_group_for_widget(widget)
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

    def _focused_widget(self) -> Widget | None:
        focused = self.focused
        return self._widget_by_focusable.get(id(focused)) if focused is not None else None

    def action_help(self) -> None:
        """Show the keyboard shortcuts as a notification."""
        lines = [f'{binding.key_display or binding.key}: {binding.description}' for binding in self.BINDINGS]
        self.notify('\n'.join(lines), title='Dashboard shortcuts', timeout=10)

    def _update_status(self, message: str | None = None) -> None:
        window = f'{self._start:%Y-%m-%d %H:%M} -> {self._end:%H:%M} UTC'
        label = self.query_one('#dash_status', Label)
        base = f'{len(self._panels)} metric panels · window {window}'
        label.update(f'{base} · {message}' if message else base)
