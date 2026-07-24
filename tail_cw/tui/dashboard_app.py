"""Textual app that renders a CloudWatch dashboard in the terminal.

The dashboard never scrolls. Every widget shows as a compact, color-coded cell
in a fit-to-screen grid (Rich sparklines). Focusing a metric promotes it to a
full plotext chart on a stage above the grid; a second focus gives a two-up;
reset returns to the even grid. Charts are drawn with native terminal cells, no
graphics protocol, so nothing ghosts. Diving from a chart or log widget opens
the existing log table for that window and log group.

The app depends only on injected callables for AWS access so it can be driven
headless in tests with no network.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar

from rich.console import Group
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Footer, Header, Input, Label, Markdown, Static

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
from tail_cw.charts import ChartKind
from tail_cw.charts.palette import MetricRole, role_color, role_for, series_color
from tail_cw.charts.sparkline import ReduceMode, build_compact, sparkline_text
from tail_cw.cli import DashboardRequest
from tail_cw.config import TailCWConfig
from tail_cw.tui.command_bar import CommandLine
from tail_cw.tui.log_results import LogResultsScreen
from tail_cw.tui.plot_widget import PlotChart

FetchMetrics = Callable[[Sequence[dict[str, object]], datetime, datetime], list[MetricSeries]]
ResolveLogs = Callable[[str, datetime, datetime], Path | None]
LogVolume = Callable[[str, datetime, datetime], list[float]]

_DEFAULT_PERIOD = 300
_CELL_HEIGHT = 5
_STAT_CYCLE = ('Average', 'Sum', 'Minimum', 'Maximum', 'p95')
_PERIOD_CYCLE = (60, 300, 900, 3600, 21600)
_RANGE_CHOICES = ('15m', '1h', '3h', '6h', '12h', '1d')
_SOURCE_RE = re.compile(r"SOURCE\s+'([^']+)'")
_DURATION_RE = re.compile(r'(\d+)([mhd])')
_DURATION_UNITS = {'m': 'minutes', 'h': 'hours', 'd': 'days'}


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


class WhichKeyScreen(ModalScreen[None]):
    """A dismissable reference of key bindings and ``:`` commands."""

    DEFAULT_CSS = """
    WhichKeyScreen {
        align: center middle;
        background: $background 60%;
    }
    WhichKeyScreen > Static {
        width: auto;
        max-width: 80%;
        height: auto;
        padding: 1 2;
        border: round $accent;
        background: $panel;
    }
    """

    BINDINGS: ClassVar[Sequence[Binding]] = [Binding('escape,comma,q,question_mark', 'dismiss', 'Close')]  # type: ignore[assignment]

    def __init__(self, keys: list[tuple[str, str]], commands: list[tuple[str, str]]) -> None:
        """Show the given key bindings and command summaries."""
        super().__init__()
        self._keys = keys
        self._commands = commands

    def compose(self) -> ComposeResult:
        """Render the reference panel.

        Yields:
            A single Static holding the keys-and-commands reference.
        """
        key_lines = '\n'.join(f'  [b]{key:<8}[/] {description}' for key, description in self._keys)
        command_lines = '\n'.join(f'  [b]:{name:<7}[/] {summary}' for name, summary in self._commands)
        body = f'[b]Keys[/]\n{key_lines}\n\n[b]Commands[/]\n{command_lines}\n\n[dim]esc to close[/]'
        yield Static(body)


@dataclass
class _Panel:
    widget: Widget
    cell: CompactPanel
    series: list[MetricSeries] = field(default_factory=list)
    stat_override: str | None = None
    period_override: int | None = None


@dataclass(frozen=True)
class _Command:
    summary: str
    args: tuple[str, ...] = ()


def _build_commands() -> dict[str, _Command]:
    return {
        'dive': _Command('Open the logs behind the focused widget'),
        'filter': _Command('Show only panels matching terms (roles or title text); "all" resets', ()),
        'focus': _Command('Focus a panel on the stage (matches by title)', ('<panel>',)),
        'help': _Command('List the available commands'),
        'period': _Command('Set the period, in seconds, of the focused metric', tuple(str(p) for p in _PERIOD_CYCLE)),
        'range': _Command('Set the time window ending now', _RANGE_CHOICES),
        'reset': _Command('Clear the stage'),
        'stat': _Command('Set the statistic of the focused metric', _STAT_CYCLE),
    }


def _panel_matches(widget: Widget, terms: list[str]) -> bool:
    """True when any term names the widget's role or appears in its title."""
    title = (getattr(widget, 'title', '') or '').lower()
    role = role_for(title)
    role_name = role.value if isinstance(role, MetricRole) else ''
    return any(term.lower() == role_name or term.lower() in title for term in terms)


def _duration(text: str) -> timedelta | None:
    match = _DURATION_RE.fullmatch(text.strip())
    if match is None:
        return None
    return timedelta(**{_DURATION_UNITS[match.group(2)]: int(match.group(1))})


class DashboardApp(App[None]):
    """Renders one dashboard as a no-scroll grid with a focus-expand stage."""

    CSS = """
    Screen {
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

    BINDINGS: ClassVar[Sequence[Binding]] = [  # type: ignore[assignment]
        Binding('q', 'quit', 'Quit'),
        Binding('h', 'nav_left', 'Left'),
        Binding('j', 'nav_down', 'Down'),
        Binding('k', 'nav_up', 'Up'),
        Binding('l', 'nav_right', 'Right'),
        Binding('enter', 'promote', 'Focus'),
        Binding('escape', 'reset_focus', 'Clear'),
        Binding('s', 'cycle_stat', 'Stat'),
        Binding('p', 'cycle_period', 'Period'),
        Binding('d', 'dive', 'Dive'),
        Binding('colon', 'command', 'Command', key_display=':'),
        Binding('comma', 'which_key', 'Keys', key_display=','),
    ]

    def __init__(
        self,
        dashboard: Dashboard,
        request: DashboardRequest,
        config: TailCWConfig,
        *,
        fetch_metrics: FetchMetrics,
        resolve_logs: ResolveLogs | None = None,
        log_volume: LogVolume | None = None,
    ) -> None:
        """Store the dashboard, its metric window, and the injected AWS callables."""
        super().__init__()
        self.title = f'Dashboard: {dashboard.name}'
        self._dashboard = dashboard
        self._config = config
        self._fetch_metrics = fetch_metrics
        self._resolve_logs = resolve_logs
        self._log_volume = log_volume
        self._start = request.start_time
        self._end = request.end_time
        self._panels: list[_Panel] = []
        self._panel_by_cell_id: dict[int, _Panel] = {}
        self._focused: list[int] = []
        self._hidden: set[int] = set()
        self._columns = 1
        self._commands = _build_commands()

    def compose(self) -> ComposeResult:
        """Build the header, the compact grid, the focus stage, and the status line.

        Yields:
            The header, the grid container, the stage container, a status label, and the footer.
        """
        yield Header(show_clock=True)
        with Container(id='grid'):
            for index, widget in enumerate(self._dashboard.widgets):
                cell = CompactPanel(index, widget)
                panel = _Panel(widget=widget, cell=cell)
                self._panels.append(panel)
                self._panel_by_cell_id[id(cell)] = panel
                yield cell
        yield Horizontal(id='stage')
        yield Label('', id='dash_status')
        yield CommandLine(completer=self._complete_command)
        yield Footer()

    def on_mount(self) -> None:
        """Size the grid, render fixed cells, fetch metrics, and focus a chart by default."""
        self._resize_grid()
        for panel in self._panels:
            self._render_fixed_cell(panel)
        default = next((panel.cell.index for panel in self._panels if isinstance(panel.widget, MetricWidget)), None)
        if default is not None:
            self._focused = [default]
        self._rebuild_stage()
        self._update_status()
        self._load_all_metrics()
        self._load_all_log_volumes()
        if self._panels:
            self._panels[0].cell.focus()

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
                source = resolve_log_group_for_widget(widget) or 'logs'
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
        if self._log_volume is None:
            return
        for panel in self._panels:
            if isinstance(panel.widget, LogWidget):
                self.run_worker(
                    lambda p=panel: self._fetch_log_volume(p),
                    name='log_volume',
                    group=f'log_volume_{panel.cell.index}',
                    exclusive=True,
                    thread=True,
                )

    def _fetch_log_volume(self, panel: _Panel) -> None:
        if self._log_volume is None or not isinstance(panel.widget, LogWidget):
            return
        source = resolve_log_group_for_widget(panel.widget) or panel.widget.title
        try:
            volume = self._log_volume(source, self._start, self._end)
        except Exception:
            return
        self.call_from_thread(self._render_log_volume, panel, volume)

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

    def _move(self, delta: int) -> None:
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
        self._focused = [panel.cell.index]
        self._rebuild_stage()

    def action_reset_focus(self) -> None:
        """Clear the stage, leaving its space reserved and empty."""
        self._focused = []
        self._rebuild_stage()

    def _rebuild_stage(self) -> None:
        stage = self.query_one('#stage', Horizontal)
        for child in list(stage.children):
            child.remove()
        for panel in self._panels:
            panel.cell.set_class(panel.cell.index in self._focused, 'staged')
        if not self._focused:
            stage.mount(Label('Select a panel and press enter to focus it here', classes='stage_hint'))
            self._update_status()
            return
        for index in self._focused:
            stage.mount(self._stage_widget(self._panels[index]))
        self._update_status()

    def _stage_widget(self, panel: _Panel) -> Any:
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
        window = f'{self._start:%H:%M}-{self._end:%H:%M}'
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

    def action_command(self) -> None:
        """Open the command line."""
        self.query_one(CommandLine).open()

    def action_which_key(self) -> None:
        """Show the keys and commands reference (nvim-style which-key)."""
        keys = [(binding.key_display or binding.key, binding.description) for binding in self.BINDINGS]
        commands = [(name, command.summary) for name, command in sorted(self._commands.items())]
        self.push_screen(WhichKeyScreen(keys, commands))

    @on(Input.Submitted, '#command_line')
    def _on_command(self, event: Input.Submitted) -> None:
        event.stop()
        line = self.query_one(CommandLine)
        text = event.value.strip()
        line.close()
        if text:
            line.remember(text)
            self._run_command(text)
        self._refocus_grid()

    def _refocus_grid(self) -> None:
        target = self._focused[0] if self._focused else 0
        if self._panels:
            self._panels[min(target, len(self._panels) - 1)].cell.focus()

    def _complete_command(self, value: str) -> list[str]:
        parts = value.split()
        if not parts or (len(parts) == 1 and not value.endswith(' ')):
            prefix = parts[0] if parts else ''
            return [name for name in self._commands if name.startswith(prefix)]
        name, command = parts[0], self._commands.get(parts[0])
        if command is None:
            return []
        arg_prefix = parts[1] if len(parts) > 1 else ''
        candidates = self._panel_titles() if command.args == ('<panel>',) else list(command.args)
        return [f'{name} {candidate}' for candidate in candidates if candidate.lower().startswith(arg_prefix.lower())]

    def _panel_titles(self) -> list[str]:
        return [title for panel in self._panels if (title := getattr(panel.widget, 'title', ''))]

    def _run_command(self, text: str) -> None:
        name, *args = text.split()
        argument = ' '.join(args)
        match name:
            case 'focus':
                self._command_focus(argument)
            case 'reset':
                self.action_reset_focus()
            case 'dive':
                self.action_dive()
            case 'stat':
                self._command_stat(argument)
            case 'period':
                self._command_period(argument)
            case 'range':
                self._command_range(argument)
            case 'filter':
                self._command_filter(args)
            case 'help':
                self._command_help()
            case _:
                self.notify(f'Unknown command: {name}', severity='warning')

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
        self._focused = [panel.cell.index]
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

    def _command_range(self, argument: str) -> None:
        delta = _duration(argument)
        if delta is None:
            self.notify('Usage: :range <15m|1h|3h|6h|12h|1d>', severity='warning')
            return
        self._end = datetime.now(tz=UTC)
        self._start = self._end - delta
        self._update_status(f'window -> last {argument}')
        self._load_all_metrics()
        self._load_all_log_volumes()

    def _command_filter(self, terms: list[str]) -> None:
        if not terms or terms[0].lower() in {'all', 'clear'}:
            self._hidden = set()
        else:
            self._hidden = {p.cell.index for p in self._panels if not _panel_matches(p.widget, terms)}
        visible_count = len(self._panels) - len(self._hidden)
        if visible_count == 0:
            self.notify(f'No panels match {" ".join(terms)!r}', severity='warning')
            self._hidden = set()
        for panel in self._panels:
            panel.cell.display = panel.cell.index not in self._hidden
        self._focused = [index for index in self._focused if index not in self._hidden]
        self._resize_grid()
        self._rebuild_stage()
        self._update_status('filtered' if self._hidden else None)

    def _command_help(self) -> None:
        lines = [f':{name} — {command.summary}' for name, command in sorted(self._commands.items())]
        self.notify('\n'.join(lines), title='Commands', timeout=12)

    def _update_status(self, message: str | None = None) -> None:
        window = f'{self._start:%Y-%m-%d %H:%M} -> {self._end:%H:%M} UTC'
        focus = f' · {len(self._focused)} focused' if self._focused else ''
        base = f'{len(self._panels)} panels · window {window}{focus}'
        self.query_one('#dash_status', Label).update(f'{base} · {message}' if message else base)


def _series_colors(series: list[MetricSeries]) -> list[str]:
    return [series_color(index) for index in range(len(series))]


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
