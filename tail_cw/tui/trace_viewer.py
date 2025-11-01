"""Trace viewer TUI screen with collapsible/expandable tree.

Provides a Textual-based interface for viewing distributed traces in a
hierarchical tree structure, grouped by service with chronological ordering.
"""

from __future__ import annotations

from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.screen import Screen
from textual.widgets import Footer, Header, Input, Label, Tree
from textual.widgets.tree import TreeNode

from tail_cw.query.trace import TraceGroup, TraceSpan, format_trace_duration
from tail_cw.tui.record_detail import RecordDetailScreen


class TraceViewerScreen(Screen[None]):
    """Screen for viewing distributed traces in a tree structure.

    Displays traces grouped by service with spans in chronological order.
    Supports search, navigation, error highlighting, and drill-down to span details.
    """

    CSS = """
    TraceViewerScreen {
        layout: vertical;
    }

    #trace_search {
        dock: top;
        height: 3;
        padding: 0 1;
        border: solid $accent;
    }

    #trace_tree {
        height: 1fr;
        width: 100%;
    }

    #trace_status {
        height: 1;
        width: 100%;
        background: $panel;
        padding: 0 1;
    }

    .error-span {
        color: $error;
    }
    """

    BINDINGS: ClassVar = [
        Binding('escape', 'close_trace_view', 'Back to Logs', show=True),
        Binding('/', 'focus_trace_search', 'Search Trace', show=True),
        Binding('e', 'expand_all', 'Expand All', show=True),
        Binding('c', 'collapse_all', 'Collapse All', show=True),
        Binding('n', 'next_error', 'Next Error', show=True),
        Binding('p', 'prev_error', 'Prev Error', show=True),
        Binding('enter', 'show_span_detail', 'Span Detail', show=True),
        Binding('?', 'help', 'Help', show=False),
    ]

    def __init__(self, trace_groups: list[TraceGroup], title: str = 'Trace Viewer') -> None:
        """Initialize trace viewer with trace groups.

        Args:
            trace_groups: List of traces to display.
            title: Screen title.
        """
        super().__init__()
        self._trace_groups = trace_groups
        self._all_trace_groups = trace_groups  # Keep original for search reset
        self._title = title
        self._tree: Tree | None = None
        self._search_input: Input | None = None
        self._status_label: Label | None = None

    def compose(self) -> ComposeResult:
        """Build the UI.

        Yields:
            Header, search input, tree container, footer.
        """
        yield Header(show_clock=True)
        yield Input(placeholder='Search traces by ID or service...', id='trace_search')
        with Container():
            yield Tree('Traces', id='trace_tree')
            yield Label('No traces loaded', id='trace_status')
        yield Footer()

    def on_mount(self) -> None:
        """Called after widgets are mounted.

        Gets widget references and builds the trace tree.
        """
        self._tree = self.query_one('#trace_tree', Tree)
        self._search_input = self.query_one('#trace_search', Input)
        self._status_label = self.query_one('#trace_status', Label)

        self._build_trace_tree()
        self._update_status()

    def _build_trace_tree(self) -> None:
        """Populate tree with trace groups.

        Creates hierarchical structure: trace root -> service nodes -> span nodes.
        """
        if not self._tree:
            return

        self._tree.clear()

        for trace_group in self._trace_groups:
            # Create trace root node
            trace_label = self._format_trace_node_label(trace_group)
            trace_node = self._tree.root.add(
                trace_label,
                data={
                    'type': 'trace',
                    'trace_id': trace_group.trace_id,
                    'trace_group': trace_group,
                },
            )

            # Group spans by service
            spans_by_service: dict[str, list[TraceSpan]] = {}
            for span in trace_group.spans:
                if span.service_name not in spans_by_service:
                    spans_by_service[span.service_name] = []
                spans_by_service[span.service_name].append(span)

            # Create service nodes
            for service_name, spans in spans_by_service.items():
                service_label = self._format_service_node_label(service_name, spans)
                service_node = trace_node.add(
                    service_label,
                    data={
                        'type': 'service',
                        'service_name': service_name,
                        'trace_id': trace_group.trace_id,
                    },
                )

                # Sort spans chronologically
                sorted_spans = sorted(spans, key=lambda s: s.log_event.timestamp)

                # Create span nodes
                for span in sorted_spans:
                    span_label = self._format_span_node_label(span)
                    service_node.add_leaf(
                        span_label,
                        data={
                            'type': 'span',
                            'span': span,
                            'trace_id': trace_group.trace_id,
                        },
                    )

                # Collapse service node by default
                service_node.collapse()

            # Expand trace root by default
            trace_node.expand()

    def _format_trace_node_label(self, trace_group: TraceGroup) -> Text:
        """Format trace root node label.

        Args:
            trace_group: The trace group.

        Returns:
            Rich Text with formatted label.
        """
        trace_id_short = trace_group.trace_id[:8]
        if len(trace_group.trace_id) > 8:
            trace_id_short += '...'

        duration_str = format_trace_duration(trace_group.duration_ms)
        service_list = ', '.join(sorted(trace_group.service_names))

        text = Text()
        text.append('Trace ', style='bold')
        text.append(trace_id_short, style='cyan')
        text.append(' | ', style='dim')
        text.append(duration_str, style='yellow')
        text.append(' | ', style='dim')
        text.append(f'{trace_group.span_count} spans', style='green')
        text.append(' | ', style='dim')
        text.append(f'Services: {service_list}', style='blue')

        if trace_group.error_count > 0:
            text.append(' | ', style='dim')
            text.append(f'⚠ {trace_group.error_count} errors', style='red bold')

        return text

    def _format_service_node_label(self, service_name: str, spans: list[TraceSpan]) -> Text:
        """Format service node label.

        Args:
            service_name: Service name.
            spans: List of spans for this service.

        Returns:
            Rich Text with formatted label.
        """
        span_count = len(spans)
        error_count = sum(1 for span in spans if span.is_error)

        # Calculate duration range
        if spans:
            durations = [s.duration_ms for s in spans if s.duration_ms is not None]
            if durations:
                min_dur = min(durations)
                max_dur = max(durations)
                duration_range = f'{format_trace_duration(min_dur)} - {format_trace_duration(max_dur)}'
            else:
                duration_range = 'N/A'
        else:
            duration_range = 'N/A'

        text = Text()
        text.append('📦 ', style='')
        text.append(service_name, style='bold blue')
        text.append(' | ', style='dim')
        text.append(f'{span_count} spans', style='green')
        text.append(' | ', style='dim')
        text.append(duration_range, style='yellow')

        if error_count > 0:
            text.append(' | ', style='dim')
            text.append(f'⚠ {error_count} errors', style='red bold')

        return text

    def _format_span_node_label(self, span: TraceSpan) -> Text:
        """Format span node label.

        Args:
            span: The trace span.

        Returns:
            Rich Text with formatted label.
        """
        timestamp_str = span.log_event.timestamp.strftime('%H:%M:%S.%f')[:-3]
        message = span.log_event.message[:80]
        if len(span.log_event.message) > 80:
            message += '...'

        duration_str = format_trace_duration(span.duration_ms) if span.duration_ms is not None else 'N/A'

        text = Text()

        if span.is_error:
            text.append('❌ ', style='red bold')

        text.append(f'[{timestamp_str}]', style='dim')
        text.append(' ', style='')
        text.append(message, style='red' if span.is_error else '')
        text.append(' | ', style='dim')
        text.append(duration_str, style='yellow')

        return text

    def _update_status(self) -> None:
        """Update status bar with trace count."""
        if not self._status_label:
            return

        trace_count = len(self._trace_groups)
        total_spans = sum(tg.span_count for tg in self._trace_groups)
        error_count = sum(tg.error_count for tg in self._trace_groups)

        status = f'{trace_count} traces | {total_spans} spans'
        if error_count > 0:
            status += f' | {error_count} errors'

        self._status_label.update(status)

    def action_close_trace_view(self) -> None:
        """Return to log table view."""
        self.dismiss()

    def action_focus_trace_search(self) -> None:
        """Focus search input."""
        if self._search_input:
            self._search_input.focus()

    def action_expand_all(self) -> None:
        """Expand all tree nodes."""
        if not self._tree:
            return

        self._tree.root.expand_all()
        if self._status_label:
            self._status_label.update('Expanded all nodes')

    def action_collapse_all(self) -> None:
        """Collapse all tree nodes except trace roots."""
        if not self._tree:
            return

        for trace_node in self._tree.root.children:
            for service_node in trace_node.children:
                service_node.collapse()

        if self._status_label:
            self._status_label.update('Collapsed all nodes')

    def action_next_error(self) -> None:
        """Jump to next error span."""
        if not self._tree:
            return

        error_nodes = self._find_error_nodes(self._tree)
        if not error_nodes:
            if self._status_label:
                self._status_label.update('No errors found')
            return

        # Find current cursor position
        current_node = self._tree.cursor_node
        current_idx = -1
        if current_node and current_node in error_nodes:
            current_idx = error_nodes.index(current_node)

        # Move to next error (wrap around)
        next_idx = (current_idx + 1) % len(error_nodes)
        next_node = error_nodes[next_idx]

        # Expand parent nodes and move cursor
        self._expand_parents(next_node)
        self._tree.select_node(next_node)

        if self._status_label:
            self._status_label.update(f'Error {next_idx + 1} of {len(error_nodes)}')

    def action_prev_error(self) -> None:
        """Jump to previous error span."""
        if not self._tree:
            return

        error_nodes = self._find_error_nodes(self._tree)
        if not error_nodes:
            if self._status_label:
                self._status_label.update('No errors found')
            return

        # Find current cursor position
        current_node = self._tree.cursor_node
        current_idx = -1
        if current_node and current_node in error_nodes:
            current_idx = error_nodes.index(current_node)

        # Move to previous error (wrap around)
        prev_idx = (current_idx - 1) % len(error_nodes)
        prev_node = error_nodes[prev_idx]

        # Expand parents and move cursor
        self._expand_parents(prev_node)
        self._tree.select_node(prev_node)

        if self._status_label:
            self._status_label.update(f'Error {prev_idx + 1} of {len(error_nodes)}')

    def action_show_span_detail(self) -> None:
        """Show detail modal for selected span."""
        if not self._tree:
            return

        node = self._tree.cursor_node
        if not node or not node.data:
            return

        if node.data.get('type') == 'span':
            span: TraceSpan = node.data['span']
            log_event = span.log_event
            self.app.push_screen(RecordDetailScreen(log_event))

    def action_help(self) -> None:
        """Show help information."""
        help_text = """
Trace Viewer Shortcuts:
  Escape      - Back to logs
  /           - Search traces
  e           - Expand all
  c           - Collapse all
  n           - Next error
  p           - Previous error
  Enter       - Show span detail
  Arrow keys  - Navigate tree
        """
        self.app.notify(help_text.strip())

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        """Handle node selection.

        Args:
            event: Node selection event.
        """
        if not self._status_label:
            return

        node = event.node
        if not node.data:
            return

        node_type = node.data.get('type')
        if node_type == 'trace':
            trace_group: TraceGroup = node.data['trace_group']
            duration_str = format_trace_duration(trace_group.duration_ms)
            self._status_label.update(
                f'Trace: {trace_group.trace_id} | {trace_group.span_count} spans | {duration_str}',
            )
        elif node_type == 'service':
            service_name = node.data['service_name']
            self._status_label.update(f'Service: {service_name}')
        elif node_type == 'span':
            span: TraceSpan = node.data['span']
            self._status_label.update(
                f'Span: {span.service_name} | {span.log_event.timestamp.strftime("%Y-%m-%d %H:%M:%S")}',
            )

    def on_input_changed(self, event: Input.Changed) -> None:
        """Handle search input changes.

        Args:
            event: Input change event.
        """
        if event.input.id != 'trace_search':
            return

        query = event.value.lower().strip()

        if not query:
            # Reset to all traces
            self._trace_groups = self._all_trace_groups
        else:
            # Filter by trace_id or service_name
            self._trace_groups = [
                tg
                for tg in self._all_trace_groups
                if query in tg.trace_id.lower() or any(query in service.lower() for service in tg.service_names)
            ]

        # Rebuild tree
        self._build_trace_tree()
        self._update_status()

        if self._status_label and query and not self._trace_groups:
            self._status_label.update(f'No matches for: {query}')

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle Enter in search.

        Args:
            event: Input submission event.
        """
        if event.input.id == 'trace_search' and self._tree:
            self._tree.focus()

    def _find_error_nodes(self, tree: Tree) -> list[TreeNode]:
        """Find all error span nodes in tree.

        Args:
            tree: The tree widget.

        Returns:
            List of error span nodes.
        """
        error_nodes = []

        def visit_node(node: TreeNode) -> None:
            if node.data and node.data.get('type') == 'span':
                span: TraceSpan = node.data['span']
                if span.is_error:
                    error_nodes.append(node)
            for child in node.children:
                visit_node(child)

        for child in tree.root.children:
            visit_node(child)

        return error_nodes

    def _expand_parents(self, node: TreeNode) -> None:
        """Expand all parent nodes to make node visible.

        Args:
            node: The node to make visible.
        """
        parent = node.parent
        while parent:
            parent.expand()
            parent = parent.parent
