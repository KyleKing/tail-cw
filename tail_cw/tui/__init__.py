"""Textual-based terminal user interface.

This module provides a high-performance TUI for viewing and searching CloudWatch
logs. It includes a DataTable widget for columnar log display, modal screens for
full record inspection, trace viewer with waterfall-style grouping across multiple
sources, and search input with live filtering. The implementation follows Textual
performance best practices including batch updates, streaming, and efficient
rendering using Rich Segments.
"""

from tail_cw.tui.app import LogTailApp
from tail_cw.tui.log_viewer import (
    batch_format_log_events,
    format_log_event_detail,
    format_log_event_for_table,
    format_timestamp,
    get_column_definitions,
)
from tail_cw.tui.record_detail import RecordDetailScreen
from tail_cw.tui.trace_viewer import TraceViewerScreen

__all__ = [
    'LogTailApp',
    'RecordDetailScreen',
    'TraceViewerScreen',
    'batch_format_log_events',
    'format_log_event_detail',
    'format_log_event_for_table',
    'format_timestamp',
    'get_column_definitions',
]
