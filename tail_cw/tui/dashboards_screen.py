"""The dashboard browser: pick a dashboard by what is on it, not by its name.

Same list-and-detail shape as the group browser. The pane on the right lists the
highlighted dashboard's widget titles so the choice is made before a full fetch
and render.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable, Input

from tail_cw.aws.dashboards import (
    AlarmWidget,
    Dashboard,
    DashboardSummary,
    LogWidget,
    MetricWidget,
    TextWidget,
    UnknownWidget,
    Widget,
)
from tail_cw.tui.navigation import NavTarget, ViewKind
from tail_cw.tui.picker import (
    DEBOUNCE_SECONDS,
    Debounce,
    FilterInput,
    Picker,
    PickerColumn,
    humanize_bytes,
    resolve_name_pattern,
)
from tail_cw.tui.shell import ShellCommand, ShellScreen

NO_LIST_SERVICE = 'No dashboard listing available. Start tail-cw with AWS credentials to browse dashboards.'
"""Shown in place of the list when no listing service is wired."""

NO_DETAIL_SERVICE = 'No dashboard body available. Start tail-cw with AWS credentials to preview widgets.'
"""Shown in place of the detail pane when no loading service is wired."""

_COLUMNS = (
    PickerColumn(key='name', label='Dashboard'),
    PickerColumn(key='size', label='Size', width=10),
)


def widget_label(widget: Widget) -> tuple[str, str]:
    """Describe a widget as a ``(kind, title)`` pair for the detail pane."""
    match widget:
        case MetricWidget():
            return (widget.view, widget.title or 'untitled metric')
        case LogWidget():
            return ('logs', widget.title or 'untitled query')
        case AlarmWidget():
            return ('alarm', widget.title or 'untitled alarm')
        case TextWidget():
            first = next((line.strip(' #') for line in widget.markdown.splitlines() if line.strip()), '')
            return ('text', first or 'empty text')
        case UnknownWidget():
            return (widget.widget_type, 'unsupported widget')


def render_dashboard_detail(dashboard: Dashboard) -> Text:
    """Render a dashboard as its name, widget count, and one line per widget."""
    body = Text()
    body.append(dashboard.name, style='bold')
    body.append(f'\n{len(dashboard.widgets)} widgets\n', style='dim')
    if not dashboard.widgets:
        body.append('\nThis dashboard has no widgets', style='dim italic')
        return body
    for widget in dashboard.widgets:
        kind, title = widget_label(widget)
        body.append(f'\n{kind:>12}  ', style='dim')
        body.append(title)
    return body


class DashboardsScreen(ShellScreen):
    """Browse the account's dashboards and open one."""

    BINDINGS: ClassVar[Sequence[Binding]] = [
        Binding('slash', 'open_filter', 'Filter', key_display='/'),
        Binding('enter', 'open_dashboard', 'Open dashboard'),
        Binding('r', 'reload', 'Reload'),
    ]

    def __init__(self, *, debounce_seconds: float = DEBOUNCE_SECONDS) -> None:
        """Start empty; everything comes off the shell once mounted.

        ``debounce_seconds`` exists so tests can make the detail timer
        deterministic; the shell always takes the default.
        """
        super().__init__()
        self._debounce_seconds = debounce_seconds
        self._summaries: list[DashboardSummary] = []
        self._visible: list[DashboardSummary] = []
        self._bodies: dict[str, Dashboard] = {}
        self._detail_debounce: Debounce | None = None

    def compose_content(self) -> ComposeResult:  # ruff: ignore[no-self-use]
        """Yield the list-and-detail picker.

        Yields:
            One Picker over the dashboard columns.
        """
        yield Picker(_COLUMNS, placeholder='/ filter dashboards (Enter keeps, Esc clears)')

    def on_mount(self) -> None:
        """Draw the breadcrumb, focus the table, and load the list."""
        super().on_mount()
        self._detail_debounce = Debounce(self, self._debounce_seconds)
        self.restore_focus()
        self._load_dashboards()

    def commands(self) -> dict[str, ShellCommand]:  # ruff: ignore[no-self-use]
        """Add the dashboard-browser commands to the shared set."""
        return {'reload': ShellCommand('Re-read the dashboard list')}

    def run_view_command(self, name: str, argument: str) -> bool:
        """Handle the dashboard-browser commands, reporting whether one matched."""
        del argument
        if name == 'reload':
            self.action_reload()
            return True
        return False

    def nav_siblings(self) -> list[NavTarget]:
        """Cycle the dashboards this view knows about."""
        return [
            NavTarget(kind=ViewKind.DASHBOARD, label=summary.name, payload=(summary.name,))
            for summary in self._summaries
        ]

    def refresh_view(self) -> None:
        """Nothing here follows the window, so only the detail pane is redrawn."""
        self._request_detail()

    def restore_focus(self) -> None:
        """Put the cursor back on the table so the view's keys work."""
        self._picker.table.focus()

    @property
    def _picker(self) -> Picker:
        return self.query_one(Picker)

    @property
    def visible_dashboards(self) -> list[str]:
        """The dashboard names the current filter leaves visible."""
        return [summary.name for summary in self._visible]

    def highlighted_dashboard(self) -> str | None:
        """The dashboard under the cursor, or None when the list is empty."""
        row = self._picker.table.cursor_row
        if 0 <= row < len(self._visible):
            return self._visible[row].name
        return None

    def action_open_filter(self) -> None:
        """Open the ``/`` filter box."""
        self._picker.open_filter()

    def action_open_dashboard(self) -> None:
        """Open the highlighted dashboard."""
        name = self.highlighted_dashboard()
        if name is None:
            self.notify('No dashboard to open', severity='warning')
            return
        self.shell.goto(NavTarget(kind=ViewKind.DASHBOARD, label=name, payload=(name,)))

    def action_reload(self) -> None:
        """Re-read the dashboard list from AWS."""
        self._summaries = []
        self._load_dashboards()

    @on(DataTable.RowHighlighted, '#picker_table')
    def _on_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        event.stop()
        self._request_detail()

    @on(DataTable.RowSelected, '#picker_table')
    def _on_row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        self.action_open_dashboard()

    @on(Input.Changed, '#picker_filter')
    def _on_filter_changed(self, event: Input.Changed) -> None:
        event.stop()
        self._apply_filter(event.value)

    @on(Input.Submitted, '#picker_filter')
    def _on_filter_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._picker.filter_input.display = False
        self.restore_focus()

    @on(FilterInput.Cancelled)
    def _on_filter_cancelled(self, event: FilterInput.Cancelled) -> None:
        event.stop()
        self._picker.filter_input.value = ''
        self._picker.close_filter()

    def _load_dashboards(self) -> None:
        if self.shell.services.list_dashboards is None:
            self._picker.show_detail(NO_LIST_SERVICE)
            self._picker.set_status(NO_LIST_SERVICE)
            return
        self.run_worker(
            self._fetch_dashboards(),
            name='list_dashboards',
            group='list_dashboards',
            exclusive=True,
        )

    async def _fetch_dashboards(self) -> None:
        list_dashboards = self.shell.services.list_dashboards
        if list_dashboards is None:  # pragma: no cover - guarded by the caller
            return
        try:
            summaries = await list_dashboards()
        except Exception as err:
            self.notify(f'Listing dashboards failed: {err}', severity='error')
            return
        self._apply_dashboards(summaries)

    def _apply_dashboards(self, summaries: list[DashboardSummary]) -> None:
        self._summaries = summaries
        self.shell.session.dashboard_names = [summary.name for summary in summaries]
        self._apply_filter(self._picker.filter_value)

    def _apply_filter(self, pattern: str) -> None:
        kept = set(resolve_name_pattern(pattern, [summary.name for summary in self._summaries]))
        self._visible = [summary for summary in self._summaries if summary.name in kept]
        table = self._picker.table
        table.clear()
        for summary in self._visible:
            table.add_row(summary.name, humanize_bytes(summary.size), key=summary.name)
        self._picker.set_status(f'{len(self._visible)} of {len(self._summaries)} dashboards')
        self._request_detail()

    def _request_detail(self) -> None:
        name = self.highlighted_dashboard()
        if name is None:
            if self._summaries:
                self._picker.show_detail('No dashboard matches this filter')
            return
        if self.shell.services.load_dashboard is None:
            self._picker.show_detail(NO_DETAIL_SERVICE)
            return
        cached = self._bodies.get(name)
        if cached is not None:
            self._picker.show_detail(render_dashboard_detail(cached))
            return
        self._picker.show_detail(f'Loading {name}...')
        if self._detail_debounce is not None:
            self._detail_debounce.schedule(lambda: self._start_detail(name))

    def _start_detail(self, name: str) -> None:
        self.run_worker(
            self._fetch_detail(name),
            name='load_dashboard',
            group='load_dashboard',
            exclusive=True,
        )

    async def _fetch_detail(self, name: str) -> None:
        load_dashboard = self.shell.services.load_dashboard
        if load_dashboard is None:  # pragma: no cover - guarded by the caller
            return
        try:
            dashboard = await load_dashboard(name)
        except Exception as err:
            self.notify(f'Loading {name} failed: {err}', severity='warning')
            return
        self._apply_detail(dashboard)

    def _apply_detail(self, dashboard: Dashboard) -> None:
        self._bodies[dashboard.name] = dashboard
        if self.highlighted_dashboard() == dashboard.name:
            self._picker.show_detail(render_dashboard_detail(dashboard))
