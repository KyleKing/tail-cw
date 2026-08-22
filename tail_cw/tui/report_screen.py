"""One view for every aggregation answer: rollups, alarms, Insights, and history.

All four are the same shape on screen, a markdown report over the shared window,
so they share a screen rather than each growing their own. What differs is the
service call that produces the markdown, which is what :data:`_LOADERS` holds.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import Label, Markdown

from tail_cw.aws.insights import DOLLARS_PER_GB, ScanEstimate, estimate_scan, validate_insights_request
from tail_cw.history import HistoryEntry, HistoryKind, append, load_history, make_entry
from tail_cw.query.report import render_alarm_markdown, render_markdown, render_rows_markdown
from tail_cw.tui.shell import ShellScreen


class ReportKind(StrEnum):
    """Which report a view shows."""

    ALARMS = 'alarms'
    HISTORY = 'history'
    INSIGHTS = 'insights'
    SUMMARY = 'summary'


ALARM_LOOKBACK = timedelta(days=14)
"""Shortest history an alarm's transition count is read over.

A one-hour window reports zero transitions for almost every alarm, which says
nothing about which one is flapping. A wider shared window wins.
"""

_HISTORY_KINDS = {
    ReportKind.ALARMS: HistoryKind.ALARMS,
    ReportKind.INSIGHTS: HistoryKind.INSIGHTS,
    ReportKind.SUMMARY: HistoryKind.SUMMARY,
}


class ReportScreen(ShellScreen):
    """Runs one aggregation and renders its markdown, recording it in the history."""

    DEFAULT_CSS = """
    ReportScreen #report {
        height: 1fr;
    }
    ReportScreen #report_status {
        height: 1;
        width: 100%;
        background: $panel;
        color: $text;
        padding: 0 1;
    }
    """

    BINDINGS: ClassVar[Sequence[Binding]] = [
        Binding('r', 'reload', 'Reload'),
        Binding('y', 'confirm', 'Run it'),
    ]

    def __init__(self, kind: ReportKind, payload: Sequence[str] = ()) -> None:
        """Show ``kind``, whose meaning of ``payload`` is the loader's own."""
        super().__init__()
        self._kind = kind
        self.payload = tuple(payload)
        self._markdown: Markdown | None = None
        self._status: Label | None = None
        self.confirmed = False
        self.awaiting_confirmation = False

    def compose_content(self) -> ComposeResult:
        """Yield the scrollable report and its status line."""
        with VerticalScroll():
            self._markdown = Markdown('', id='report')
            yield self._markdown
        self._status = Label('', id='report_status')
        yield self._status

    def on_mount(self) -> None:
        """Draw the breadcrumb, then run the report."""
        super().on_mount()
        self.action_reload()

    def action_reload(self) -> None:
        """Run the report again over the current shared window."""
        self.run_worker(self._load(), name='report', group='report', exclusive=True)

    def action_confirm(self) -> None:
        """Approve a query the estimate stopped, and run it."""
        if not self.awaiting_confirmation:
            return
        self.confirmed = True
        self.action_reload()

    def refresh_view(self) -> None:
        """Re-run after the shared window or filter changed."""
        self.action_reload()

    async def _load(self) -> None:
        self._set_status('Running...')
        self.awaiting_confirmation = False
        try:
            body = await _LOADERS[self._kind](self)
        except Exception as err:
            self._show(f'# {self._kind.value}\n\n{err}\n')
            self._set_status(f'Failed: {err}')
            return
        self._show(body)
        if self.awaiting_confirmation:
            self._set_status('y to run it · esc to go back')
            return
        self._set_status(f'{self._kind.value} · {self.shell.session.window_label()}')
        self._remember(body)

    def _show(self, body: str) -> None:
        if self._markdown is not None:
            self._markdown.update(body)

    def _set_status(self, text: str) -> None:
        if self._status is not None:
            self._status.update(text)

    def _title(self) -> str:
        """Name what ran: the query for Insights, the groups for a rollup."""
        if self.payload:
            return ' '.join(self.payload)
        groups = self.shell.session.selected_groups
        return f'{self._kind.value} of {", ".join(groups)}' if groups else self._kind.value

    def _remember(self, body: str) -> None:
        history_kind = _HISTORY_KINDS.get(self._kind)
        if history_kind is None:
            return
        session = self.shell.session
        append(
            make_entry(
                history_kind,
                recorded=datetime.now(tz=UTC),
                title=self._title(),
                window=session.window_label(),
                detail=body,
                profile=session.profile,
            ),
        )


async def _load_summary(screen: ReportScreen) -> str:
    roll_up = screen.shell.services.roll_up_logs
    if roll_up is None:
        return 'No rollup service available.\n'
    session = screen.shell.session
    groups = screen.shell.session.selected_groups
    if not groups:
        return 'Select log groups first, then run the rollup.\n'
    report = await roll_up(groups, session.start, session.end)
    return render_markdown(
        report,
        title='Warning-and-above patterns',
        window_label=session.window_label(),
        source=f'{len(groups)} groups',
    )


async def _load_alarms(screen: ReportScreen) -> str:
    list_alarms = screen.shell.services.list_alarms
    if list_alarms is None:
        return 'No alarm service available.\n'
    session = screen.shell.session
    start = min(session.start, session.end - ALARM_LOOKBACK)
    alarms, transitions = await list_alarms(start, session.end)
    if not alarms:
        return 'No alarms in this account.\n'
    changed = sum(1 for count in transitions.values() if count)
    header = (
        f'# Alarms\n\n{len(alarms)} alarms, {changed} of them changed state '
        f'between {start.isoformat()} and {session.end.isoformat()}\n\n'
    )
    return header + render_alarm_markdown(alarms, transitions)


async def _load_insights(screen: ReportScreen) -> str:
    run_insights = screen.shell.services.run_insights
    if run_insights is None:
        return 'No Insights service available.\n'
    query = ' '.join(screen.payload)
    session = screen.shell.session
    groups = session.selected_groups
    if not groups:
        return 'Select log groups first: Insights needs to know what to read.\n'
    validate_insights_request(query, session.start, session.end)
    estimate = await _estimate_scan(screen, groups)
    ceiling = screen.shell.config_data.insights.confirm_above_gb
    if estimate is not None and estimate.gigabytes > ceiling and not screen.confirmed:
        screen.awaiting_confirmation = True
        return f'# {query}\n\n{estimate.label()}\n\nAbove the {ceiling:g} GB ceiling.\n'
    preflight = f'{estimate.label()}\n\n' if estimate is not None else ''
    result = await run_insights(groups, query, session.start, session.end)
    scanned = result.bytes_scanned / 1_000_000_000
    header = (
        f'# {query}\n\n{preflight}'
        f'{result.records_matched:,} of {result.records_scanned:,} records matched, '
        f'{scanned:.3f} GB scanned (billed at roughly ${scanned * DOLLARS_PER_GB:.3f})\n\n'
    )
    return header + render_rows_markdown(result.columns, result.rows)


async def _estimate_scan(screen: ReportScreen, groups: Sequence[str]) -> ScanEstimate | None:
    """Estimate the query's bill from group metadata, or None when it is unavailable.

    A missing estimate does not stop the query: the typed query is still the
    caller's confirmation, and refusing to run without a number nobody can
    supply would be worse than running.
    """
    list_groups = screen.shell.services.list_groups
    if list_groups is None:
        return None
    selected = set(groups)
    session = screen.shell.session
    known = [group for group in await list_groups() if group.name in selected]
    return estimate_scan(known, window=session.end - session.start, now=datetime.now(tz=UTC))


async def _load_history(_screen: ReportScreen) -> str:  # noqa: RUF029 - conforms to the loader signature
    entries = load_history()
    if not entries:
        return 'Nothing recorded yet. Rollups, alarm reads, and Insights queries land here.\n'
    return '# History\n\n' + '\n'.join(_history_section(entry) for entry in entries)


def _history_section(entry: HistoryEntry) -> str:
    return f'## {entry.kind.value} · {entry.recorded}\n\n{entry.title}\n\n{entry.window}\n\n{entry.detail}\n'


_LOADERS: dict[ReportKind, Callable[[ReportScreen], Awaitable[str]]] = {
    ReportKind.ALARMS: _load_alarms,
    ReportKind.HISTORY: _load_history,
    ReportKind.INSIGHTS: _load_insights,
    ReportKind.SUMMARY: _load_summary,
}
