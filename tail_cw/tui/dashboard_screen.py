"""The dashboard view: one CloudWatch dashboard as a no-scroll grid.

Every widget shows as a compact, color-coded cell in a fit-to-screen grid (Rich
sparklines). Focusing a metric promotes it to a full plotext chart on a stage
above the grid, ``:add`` gives a two-up, and reset returns to the even grid.
Charts are drawn with native terminal cells, no graphics protocol, so nothing
ghosts. Diving from a chart or log widget ranks the log groups behind the widget
and asks which one to open.

The screen reaches AWS only through the shell's injected services, so it can be
driven headless with no network.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar

from rich.console import Group
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.widgets import Label, Markdown, Static

from tail_cw.aws.dashboards import (
    AlarmWidget,
    Dashboard,
    DiveCandidate,
    LogWidget,
    MetricWidget,
    TextWidget,
    UnknownWidget,
    Widget,
    candidate_log_groups,
    rank_dive_candidates,
)
from tail_cw.aws.metrics import MetricSeries, build_metric_data_queries
from tail_cw.charts import ChartKind
from tail_cw.charts.palette import MetricRole, role_color, role_for, series_color
from tail_cw.charts.sparkline import ReduceMode, build_compact, sparkline_text
from tail_cw.tui.navigation import NavTarget, ViewKind
from tail_cw.tui.plot_widget import PlotChart
from tail_cw.tui.shell import ShellCommand, ShellScreen

_DEFAULT_PERIOD = 300
_CELL_HEIGHT = 5
_MAX_STAGED = 2
_STAT_CYCLE = ('Average', 'Sum', 'Minimum', 'Maximum', 'p95')
_PERIOD_CYCLE = (60, 300, 900, 3600, 21600)


def _grid_dimensions(count: int) -> tuple[int, int]:
    if count <= 1:
        return 1, 1
    columns = 3 if count <= 9 else 4  # ruff: ignore[magic-value-comparison]
    return columns, math.ceil(count / columns)


def _panel_matches(widget: Widget, terms: Sequence[str]) -> bool:
    """True when any term names the widget's role or appears in its title."""
    title = (getattr(widget, 'title', '') or '').lower()
    role = role_for(title)
    role_name = role.value if isinstance(role, MetricRole) else ''
    return any(term.lower() == role_name or term.lower() in title for term in terms)


def _series_colors(series: list[MetricSeries]) -> list[str]:
    return [series_color(index) for index in range(len(series))]


def _widget_source(widget: Widget) -> str | None:
    candidates = candidate_log_groups(widget)
    return candidates[0][0] if candidates else None


def _full_content(widget: Widget) -> Static | Markdown:
    match widget:
        case LogWidget():
            accent = role_color(widget.title or 'logs')
            return Static(f'[bold {accent}]{widget.title or "Logs"}[/]\n\n{widget.query}')
        case TextWidget():
            return Markdown(widget.markdown)
        case AlarmWidget():
            names = '\n'.join(alarm.rsplit(':', 1)[-1] for alarm in widget.alarms) or '(no alarms)'
            return Static(f'[bold]{widget.title or "Alarms"}[/]\n\n{names}')
        case _:
            return Static('[dim]Nothing to expand[/]')


class CompactPanel(Static):
    """A focusable overview cell rendering a widget compactly (no image)."""

    DEFAULT_CSS = """
    CompactPanel {
        height: 100%;
        width: 100%;
        border: round $panel-lighten-1;
        padding: 0 1;
        overflow: hidden;
        scrollbar-size: 0 0;
    }
    CompactPanel:focus {
        border: round $accent;
    }
    CompactPanel.staged {
        border: round $success;
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


class DashboardScreen(ShellScreen):
    """Renders one dashboard as a no-scroll grid with a focus-expand stage."""

    DEFAULT_CSS = """
    DashboardScreen {
        scrollbar-size: 0 0;
    }
    #grid {
        layout: grid;
        grid-gutter: 1;
        height: auto;
        overflow: hidden;
        scrollbar-size: 0 0;
    }
    #stage {
        height: 1fr;
        overflow: hidden;
        scrollbar-size: 0 0;
    }
    #stage Vertical {
        width: 1fr;
    }
    .chart_caption {
        height: 1;
        padding: 0 1;
        color: $text-muted;
        text-style: italic;
    }
    .stage_hint {
        width: 1fr;
        height: 1fr;
        content-align: center middle;
        color: $text-muted;
    }
    #dash_status {
        dock: bottom;
        height: 1;
        background: $panel;
        color: $text;
        padding: 0 1;
    }
    """

    BINDINGS: ClassVar[Sequence[Binding]] = [
        Binding('h', 'nav_left', 'Left'),
        Binding('j', 'nav_down', 'Down'),
        Binding('k', 'nav_up', 'Up'),
        Binding('l', 'nav_right', 'Right'),
        Binding('enter', 'promote', 'Focus'),
        Binding('escape', 'reset_focus', 'Clear the stage, else back'),
        Binding('s', 'cycle_stat', 'Stat'),
        Binding('p', 'cycle_period', 'Period'),
        Binding('d', 'dive', 'Dive'),
    ]

    def __init__(self, dashboard_name: str) -> None:
        """Name the dashboard to load once the screen mounts."""
        super().__init__()
        self.dashboard_name = dashboard_name
        self._panels: list[_Panel] = []
        self._panel_by_cell_id: dict[int, _Panel] = {}
        self._staged: list[int] = []
        self._hidden: set[int] = set()
        self._columns = 1

    def compose_content(self) -> ComposeResult:
        """Build the compact grid, the focus stage, and the status line.

        Yields:
            The (initially empty) grid container, the stage container, and a status label.
        """
        yield Container(id='grid')
        yield Horizontal(id='stage')
        yield Label(f'Loading {self.dashboard_name}...', id='dash_status')

    def on_mount(self) -> None:
        """Draw the breadcrumb and load the dashboard off the message loop."""
        super().on_mount()
        self.run_worker(self._load_dashboard(), name='dashboard_load', group='dashboard_load')

    async def _load_dashboard(self) -> None:
        load = self.shell.services.load_dashboard
        if load is None:
            self._show_status('no dashboard pipeline wired')
            return
        try:
            dashboard = await load(self.dashboard_name)
        except Exception as err:
            self._show_status(f'load failed: {err}')
            return
        self.build_grid(dashboard)

    def _show_status(self, message: str) -> None:
        self.query_one('#dash_status', Label).update(message)

    def build_grid(self, dashboard: Dashboard) -> None:
        """Mount one cell per widget, then start every fetch the grid needs."""
        grid = self.query_one('#grid', Container)
        cells = []
        for index, widget in enumerate(dashboard.widgets):
            cell = CompactPanel(index, widget)
            panel = _Panel(widget=widget, cell=cell)
            self._panels.append(panel)
            self._panel_by_cell_id[id(cell)] = panel
            cells.append(cell)
        grid.mount(*cells)
        self._resize_grid()
        for panel in self._panels:
            self._render_fixed_cell(panel)
        default = next((panel.cell.index for panel in self._panels if isinstance(panel.widget, MetricWidget)), None)
        if default is not None:
            self._staged = [default]
        self._rebuild_stage()
        self._update_status()
        self._load_all_metrics()
        self._load_all_log_volumes()
        self.restore_focus()

    def _resize_grid(self) -> None:
        visible = max(1, len(self._panels) - len(self._hidden))
        columns, rows = _grid_dimensions(visible)
        self._columns = columns
        grid = self.query_one('#grid')
        grid.styles.grid_size_columns = columns
        grid.styles.grid_size_rows = rows
        grid.styles.height = rows * _CELL_HEIGHT + (rows - 1)

    def on_resize(self) -> None:
        """Keep the grid sized to the terminal and re-render compact cells."""
        if not self._panels:
            return
        self._resize_grid()
        for panel in self._panels:
            if isinstance(panel.widget, MetricWidget) and panel.series:
                panel.cell.set_series(panel.series)

    @staticmethod
    def _render_fixed_cell(panel: _Panel) -> None:
        widget = panel.widget
        match widget:
            case LogWidget():
                accent = role_color(widget.title or 'logs')
                source = _widget_source(widget) or 'logs'
                panel.cell.render_static(f'[bold {accent}]{widget.title or "Logs"}[/]\n[dim]{source}[/]')
            case TextWidget():
                heading = next((line for line in widget.markdown.splitlines() if line.strip()), '')
                panel.cell.render_static(f'[dim]{heading.lstrip("# ").strip()[:60]}[/]')
            case AlarmWidget():
                accent = role_color(widget.title or 'alarms')
                count = len(widget.alarms)
                panel.cell.render_static(f'[bold {accent}]{widget.title or "Alarms"}[/]\n[dim]{count} alarms[/]')
            case UnknownWidget():
                panel.cell.render_static(f'[dim]Unsupported widget: {widget.widget_type}[/]')
            case _:
                pass

    def _load_all_metrics(self) -> None:
        for panel in self._panels:
            if isinstance(panel.widget, MetricWidget):
                self._load_panel(panel)

    def _load_all_log_volumes(self) -> None:
        if self.shell.services.log_volume is None:
            return
        for panel in self._panels:
            if isinstance(panel.widget, LogWidget):
                self.run_worker(
                    self._fetch_log_volume(panel),
                    name='log_volume',
                    group=f'log_volume_{panel.cell.index}',
                    exclusive=True,
                )

    async def _fetch_log_volume(self, panel: _Panel) -> None:
        log_volume = self.shell.services.log_volume
        if log_volume is None or not isinstance(panel.widget, LogWidget):
            return
        source = _widget_source(panel.widget) or panel.widget.title
        session = self.shell.session
        try:
            volume = await log_volume(source, session.start, session.end)
        except Exception:
            return
        self._render_log_volume(panel, volume)

    @staticmethod
    def _render_log_volume(panel: _Panel, volume: list[float]) -> None:
        title = getattr(panel.widget, 'title', '') or 'Logs'
        accent = role_color(title)
        latest = int(volume[-1]) if volume else 0
        header = Text.assemble((title, f'bold {accent}'), ('  ', ''), (f'{latest}/bucket', 'dim'))
        spark = sparkline_text(volume, color=accent, width=max(1, panel.cell.content_size.width), bars=True)
        panel.cell.render_static(Group(header, spark))

    def _load_panel(self, panel: _Panel) -> None:
        self.run_worker(
            self._fetch_panel(panel),
            name='metric_fetch',
            group=f'metric_fetch_{panel.cell.index}',
            exclusive=True,
        )

    async def _fetch_panel(self, panel: _Panel) -> None:
        widget = panel.widget
        fetch_metrics = self.shell.services.fetch_metrics
        if fetch_metrics is None or not isinstance(widget, MetricWidget):
            return
        queries = build_metric_data_queries(
            widget.metrics,
            widget_stat=panel.stat_override or widget.stat,
            widget_period=panel.period_override or widget.period,
            default_period=_DEFAULT_PERIOD,
        )
        if not queries:
            return
        session = self.shell.session
        try:
            series = await fetch_metrics(queries, session.start, session.end)
        except Exception as err:
            panel.cell.render_static(f'[red]error:[/] {err}')
            return
        visible = [item for item in series if item.values]
        panel.series = visible
        panel.cell.set_series(visible)
        if panel.cell.index in self._staged:
            self._rebuild_stage()

    def _focused_panel(self) -> _Panel | None:
        return self._panel_by_cell_id.get(id(self.focused)) if self.focused is not None else None

    def _move(self, delta: int) -> None:
        if not self._panels:
            return
        panel = self._focused_panel()
        current = panel.cell.index if panel is not None else 0
        target = max(0, min(len(self._panels) - 1, current + delta))
        self._panels[target].cell.focus()

    def action_nav_left(self) -> None:
        """Focus the cell to the left."""
        self._move(-1)

    def action_nav_right(self) -> None:
        """Focus the cell to the right."""
        self._move(1)

    def action_nav_down(self) -> None:
        """Focus the cell one row down."""
        self._move(self._columns)

    def action_nav_up(self) -> None:
        """Focus the cell one row up."""
        self._move(-self._columns)

    def action_promote(self) -> None:
        """Put the selected panel on the stage."""
        panel = self._focused_panel()
        if panel is None:
            return
        self._staged = [panel.cell.index]
        self._rebuild_stage()

    def action_reset_focus(self) -> None:
        """Clear the stage, or go up one level when the stage is already clear."""
        if not self._staged:
            self.shell.nav_pop()
            return
        self._staged = []
        self._rebuild_stage()

    def _rebuild_stage(self) -> None:
        stage = self.query_one('#stage', Horizontal)
        for child in list(stage.children):
            child.remove()
        for panel in self._panels:
            panel.cell.set_class(panel.cell.index in self._staged, 'staged')
        if not self._staged:
            stage.mount(Label('Select a panel and press enter to focus it here', classes='stage_hint'))
            self._update_status()
            return
        for index in self._staged:
            stage.mount(self._stage_widget(self._panels[index]))
        self._update_status()

    def _stage_widget(self, panel: _Panel) -> Vertical | VerticalScroll:
        widget = panel.widget
        if isinstance(widget, MetricWidget):
            kind = ChartKind.BAR if widget.view == 'bar' else ChartKind.LINE
            colors = [role_color(widget.title)] if len(panel.series) == 1 else _series_colors(panel.series)
            chart = PlotChart(title=widget.title or '(untitled)', kind=kind, colors=colors)
            chart.set_series(panel.series)
            caption = Label(self._metric_caption(panel), classes='chart_caption')
            return Vertical(caption, chart)
        return VerticalScroll(_full_content(widget))

    def _metric_caption(self, panel: _Panel) -> str:
        widget = panel.widget
        if not isinstance(widget, MetricWidget):
            return ''
        session = self.shell.session
        window = f'{session.start:%H:%M}-{session.end:%H:%M}'
        queries = build_metric_data_queries(
            widget.metrics,
            widget_stat=panel.stat_override or widget.stat,
            widget_period=panel.period_override or widget.period,
            default_period=_DEFAULT_PERIOD,
        )
        stat = next((q['MetricStat']['Metric'] for q in queries if 'MetricStat' in q), None)
        source = next(q['MetricStat'] for q in queries if 'MetricStat' in q) if stat else None
        if stat is None or source is None:
            return f'metric math · {window}'
        dimensions = ', '.join(f'{dim["Name"]}={dim["Value"]}' for dim in stat['Dimensions'])
        parts = [stat['Namespace'], stat['MetricName']]
        if dimensions:
            parts.append(dimensions)
        parts.append(f'{source["Stat"]} · {source["Period"]}s · {window}')
        return ' · '.join(parts)

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

    def action_dive(self) -> None:
        """Rank the log groups behind the focused widget and ask which to open."""
        panel = self._focused_panel()
        if panel is None:
            return
        if not candidate_log_groups(panel.widget):
            self.notify('No log source resolved for this widget', severity='warning')
            return
        widget = panel.widget
        self.notify('Ranking log groups...', severity='information')
        self.run_worker(
            self._dive_worker(widget),
            name='dive_rank',
            group='dive_rank',
            exclusive=True,
        )

    async def _dive_worker(self, widget: Widget) -> None:
        try:
            candidates = await self._rank_candidates(widget)
        except Exception as err:
            self.notify(f'Dive ranking failed: {err}', severity='error')
            return
        self.shell.dive(widget, candidates)

    async def _count_candidates(self, widget: Widget, known: Collection[str]) -> dict[str, int]:
        count_events = self.shell.services.count_events
        groups = {log_group for log_group, _ in candidate_log_groups(widget) if log_group in known}
        if count_events is None or not groups:
            return {}
        session = self.shell.session

        async def count(log_group: str) -> int:
            return await count_events(log_group, session.start, session.end)

        async with asyncio.TaskGroup() as task_group:
            counts = {log_group: task_group.create_task(count(log_group)) for log_group in groups}
        return {log_group: task.result() for log_group, task in counts.items()}

    async def _rank_candidates(self, widget: Widget) -> list[DiveCandidate]:
        services = self.shell.services
        known = {info.name for info in await services.list_groups()} if services.list_groups is not None else set()
        counts = await self._count_candidates(widget, known)
        return rank_dive_candidates(widget, known_groups=known, count_events=lambda group: counts.get(group, 0))

    def commands(self) -> dict[str, ShellCommand]:
        """The dashboard's own ``:`` commands."""
        titles = tuple(self._panel_titles())
        return {
            'add': ShellCommand('Add a panel beside the staged one (two-up)', titles),
            'dive': ShellCommand('Rank and open the logs behind the focused widget'),
            'focus': ShellCommand('Focus a panel on the stage (matches by title)', titles),
            'panels': ShellCommand('Show only panels matching terms (roles or title text); "all" resets'),
            'period': ShellCommand(
                'Set the period, in seconds, of the focused metric',
                tuple(str(period) for period in _PERIOD_CYCLE),
            ),
            'reset': ShellCommand('Clear the stage'),
            'stat': ShellCommand('Set the statistic of the focused metric', _STAT_CYCLE),
        }

    def run_view_command(self, name: str, argument: str) -> bool:
        """Run one of the dashboard's commands, returning False for anything else."""
        match name:
            case 'focus':
                self._command_focus(argument)
            case 'add':
                self._command_add(argument)
            case 'reset':
                self._staged = []
                self._rebuild_stage()
            case 'dive':
                self.action_dive()
            case 'stat':
                self._command_stat(argument)
            case 'period':
                self._command_period(argument)
            case 'panels':
                self._command_panels(argument.split())
            case _:
                return False
        return True

    def nav_siblings(self) -> list[NavTarget]:
        """Every dashboard in the account, so ``[`` and ``]`` cycle them."""
        names = list(self.shell.session.dashboard_names)
        if self.dashboard_name not in names:
            names.insert(0, self.dashboard_name)
        return [NavTarget(kind=ViewKind.DASHBOARD, label=name, payload=(name,)) for name in names]

    def refresh_view(self) -> None:
        """Re-fetch every panel after the shared window or filter changed."""
        if not self._panels:
            return
        self._load_all_metrics()
        self._load_all_log_volumes()
        self._rebuild_stage()

    def restore_focus(self) -> None:
        """Return focus to the grid, on the staged cell when there is one."""
        if not self._panels:
            return
        target = self._staged[0] if self._staged else 0
        self._panels[min(target, len(self._panels) - 1)].cell.focus()

    def _panel_titles(self) -> list[str]:
        return [title for panel in self._panels if (title := getattr(panel.widget, 'title', ''))]

    def _find_panel(self, substring: str) -> _Panel | None:
        needle = substring.lower()
        for panel in self._panels:
            title = getattr(panel.widget, 'title', '') or ''
            if needle in title.lower():
                return panel
        return None

    def _command_focus(self, argument: str) -> None:
        panel = self._find_panel(argument)
        if panel is None:
            self.notify(f'No panel matching {argument!r}', severity='warning')
            return
        self._staged = [panel.cell.index]
        self._rebuild_stage()

    def _command_add(self, argument: str) -> None:
        panel = self._find_panel(argument)
        if panel is None:
            self.notify(f'No panel matching {argument!r}', severity='warning')
            return
        if panel.cell.index in self._staged:
            return
        self._staged = [*self._staged, panel.cell.index][-_MAX_STAGED:]
        self._rebuild_stage()

    def _command_stat(self, argument: str) -> None:
        panel = self._focused_panel()
        if panel is None or not isinstance(panel.widget, MetricWidget) or not argument:
            self.notify('Focus a metric chart, then :stat <statistic>', severity='information')
            return
        panel.stat_override = argument
        self._update_status(f'{panel.widget.title}: stat -> {argument}')
        self._load_panel(panel)

    def _command_period(self, argument: str) -> None:
        panel = self._focused_panel()
        if panel is None or not isinstance(panel.widget, MetricWidget) or not argument.isdigit():
            self.notify('Focus a metric chart, then :period <seconds>', severity='information')
            return
        panel.period_override = int(argument)
        self._update_status(f'{panel.widget.title}: period -> {argument}s')
        self._load_panel(panel)

    def _command_panels(self, terms: list[str]) -> None:
        if not terms or terms[0].lower() in {'all', 'clear'}:
            self._hidden = set()
        else:
            self._hidden = {p.cell.index for p in self._panels if not _panel_matches(p.widget, terms)}
        if len(self._panels) - len(self._hidden) == 0:
            self.notify(f'No panels match {" ".join(terms)!r}', severity='warning')
            self._hidden = set()
        for panel in self._panels:
            panel.cell.display = panel.cell.index not in self._hidden
        self._staged = [index for index in self._staged if index not in self._hidden]
        self._resize_grid()
        self._rebuild_stage()
        self._update_status('filtered' if self._hidden else None)

    def _update_status(self, message: str | None = None) -> None:
        session = self.shell.session
        window = _window_label(session.start, session.end)
        focus = f' · {len(self._staged)} focused' if self._staged else ''
        base = f'{len(self._panels)} panels · window {window}{focus}'
        self.query_one('#dash_status', Label).update(f'{base} · {message}' if message else base)


def _window_label(start: datetime, end: datetime) -> str:
    return f'{start:%Y-%m-%d %H:%M} -> {end:%H:%M} UTC'
