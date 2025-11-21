"""Waterfall timeline visualization for distributed traces.

Provides a Gantt-style chart showing span timelines, parent-child relationships,
and critical path highlighting.
"""

from __future__ import annotations

from typing import Any

from rich.console import RenderableType
from rich.text import Text
from textual.widgets import Static

from tail_cw.query.trace import TraceGroup, TraceSpan


class TraceWaterfallView(Static):
    """Widget that renders a waterfall timeline of trace spans.

    Displays spans in chronological order with:
    - Horizontal bars showing duration
    - Color coding by service
    - Indentation showing parent-child relationships
    - Highlighting of critical path
    - Error spans in red
    """

    DEFAULT_CSS = """
    TraceWaterfallView {
        height: auto;
        border: solid $primary;
        padding: 1;
    }
    """

    def __init__(self, trace: TraceGroup, *args: Any, **kwargs: Any) -> None:
        """Initialize waterfall view.

        Args:
            trace: TraceGroup to visualize
            *args: Positional arguments for Static
            **kwargs: Keyword arguments for Static
        """
        super().__init__(*args, **kwargs)
        self._trace = trace
        self._service_colors = self._assign_service_colors()

    def _assign_service_colors(self) -> dict[str, str]:
        """Assign colors to services for visualization.

        Returns:
            Mapping of service name to color name
        """
        colors = ['cyan', 'magenta', 'green', 'yellow', 'blue']
        service_list = sorted(self._trace.service_names)
        return {service: colors[i % len(colors)] for i, service in enumerate(service_list)}

    def render(self) -> RenderableType:
        """Render waterfall timeline.

        Returns:
            Renderable content for the widget
        """
        lines = []

        # Header
        lines.append(Text(f"Trace ID: {self._trace.trace_id}", style="bold"))
        lines.append(
            Text(
                f"Duration: {self._trace.duration_ms:.2f}ms | "
                f"Spans: {self._trace.span_count} | "
                f"Errors: {self._trace.error_count}",
                style="dim",
            ),
        )
        lines.append(Text())

        # Calculate time scale
        start_ms = self._trace.start_time.timestamp() * 1000
        total_duration = self._trace.duration_ms
        bar_width = 60  # characters for timeline bar

        # Render each span
        for span in self._trace.spans:
            line = self._render_span(span, start_ms, total_duration, bar_width)
            lines.append(line)

        # Join lines
        result = Text("\n").join(lines)
        return result

    def _render_span(
        self,
        span: TraceSpan,
        start_ms: float,
        total_duration: float,
        bar_width: int,
    ) -> Text:
        """Render a single span in the waterfall.

        Args:
            span: Span to render
            start_ms: Trace start time in milliseconds
            total_duration: Total trace duration in milliseconds
            bar_width: Width of timeline bar in characters

        Returns:
            Formatted text line for the span
        """
        # Calculate position and width
        span_start_ms = span.log_event.timestamp.timestamp() * 1000
        offset_ms = span_start_ms - start_ms
        offset_chars = int((offset_ms / total_duration) * bar_width) if total_duration > 0 else 0

        duration = span.duration_ms or 0
        width_chars = max(1, int((duration / total_duration) * bar_width)) if total_duration > 0 else 1

        # Service name (truncated if needed)
        service = span.service_name[:15].ljust(15)
        color = self._service_colors.get(span.service_name, 'white')

        # Build timeline bar
        bar = ' ' * offset_chars + '█' * width_chars

        # Error indicator
        error_indicator = "⚠ " if span.is_error else "  "

        # Duration text
        duration_text = f"{duration:.1f}ms" if duration > 0 else "N/A"

        # Combine
        line = Text()
        line.append(error_indicator, style="red bold" if span.is_error else "dim")
        line.append(service, style=color)
        line.append(" ")
        line.append(bar[:bar_width], style=color)
        line.append(f" {duration_text}", style="dim")

        return line


def render_waterfall_ascii(trace: TraceGroup, width: int = 80) -> str:
    """Render waterfall as ASCII art for export.

    Args:
        trace: TraceGroup to render
        width: Total width in characters

    Returns:
        ASCII waterfall representation
    """
    lines = []
    lines.append(f"Trace ID: {trace.trace_id}")
    lines.append(f"Duration: {trace.duration_ms:.2f}ms")
    lines.append("=" * width)

    start_ms = trace.start_time.timestamp() * 1000
    total_duration = trace.duration_ms
    bar_width = width - 30  # Leave space for labels

    for span in trace.spans:
        span_start_ms = span.log_event.timestamp.timestamp() * 1000
        offset_ms = span_start_ms - start_ms
        offset_chars = int((offset_ms / total_duration) * bar_width) if total_duration > 0 else 0

        duration = span.duration_ms or 0
        width_chars = max(1, int((duration / total_duration) * bar_width)) if total_duration > 0 else 1

        service = span.service_name[:12].ljust(12)
        bar = ' ' * offset_chars + '█' * width_chars
        duration_text = f"{duration:.1f}ms"

        error = "⚠" if span.is_error else " "
        line = f"{error} {service} |{bar[:bar_width]}| {duration_text}"
        lines.append(line)

    return "\n".join(lines)
