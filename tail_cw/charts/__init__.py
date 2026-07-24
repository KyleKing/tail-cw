"""Chart rendering for the dashboard TUI.

Charts are drawn natively with Unicode cells (Rich sparklines for compact cells,
plotext for focused charts), so nothing depends on a terminal graphics protocol.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ['ChartKind']


class ChartKind(StrEnum):
    """Supported chart shapes mapped from CloudWatch widget views."""

    LINE = 'line'
    BAR = 'bar'
