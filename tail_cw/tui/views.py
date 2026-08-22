"""Maps navigation targets to view screens.

The views subclass ``ShellScreen``, so ``shell`` cannot import them without a
cycle. This module sits above both and supplies the factory the app takes as a
constructor argument.
"""

from __future__ import annotations

from tail_cw.tui.dashboard_screen import DashboardScreen
from tail_cw.tui.dashboards_screen import DashboardsScreen
from tail_cw.tui.groups_screen import GroupsScreen
from tail_cw.tui.logs_screen import LogsScreen
from tail_cw.tui.navigation import NavTarget, ViewKind
from tail_cw.tui.report_screen import ReportKind, ReportScreen
from tail_cw.tui.shell import ShellScreen
from tail_cw.tui.waterfall_screen import WaterfallScreen


def build_screen(target: NavTarget) -> ShellScreen:
    """Construct the view a navigation target names."""
    match target.kind:
        case ViewKind.GROUPS:
            return GroupsScreen()
        case ViewKind.LOGS:
            return LogsScreen(
                list(target.payload),
                live=target.label.startswith('tail'),
                trace_id=target.argument or None,
            )
        case ViewKind.DASHBOARDS:
            return DashboardsScreen()
        case ViewKind.DASHBOARD:
            return DashboardScreen(target.payload[0] if target.payload else target.label)
        case ViewKind.XRAY:
            return WaterfallScreen(target.argument)
        case ViewKind.REPORT:
            # The first payload item names the report; the rest is its argument.
            return ReportScreen(ReportKind(target.payload[0]), target.payload[1:])
