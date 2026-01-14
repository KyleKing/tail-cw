# Branch 2: Distributed Tracing Enhancements - Implementation Plan

**Branch Name:** `claude/distributed-tracing-enhancements-01VGBEGD5jDcQmxrbdNnW9zA`

**Goal:** Enhance distributed tracing capabilities with waterfall timeline visualization, service map view, Jaeger export, and improved error propagation tracking.

---

## Overview

This branch implements the "Focus on Distributed Tracing" strategy from PROJECT_REVIEW.md, positioning tail-cw as a lightweight distributed tracing viewer that doesn't require a full OpenTelemetry backend.

### Key Features to Implement

1. **Waterfall Timeline Visualization** - Gantt-style chart showing span timeline
2. **Service Map View** - Visual representation of service-to-service interactions
3. **Jaeger Export** - Export traces in Jaeger JSON format
4. **Enhanced Error Propagation** - Track how errors propagate through services

---

## Implementation Steps

### Step 1: Create Waterfall Timeline Visualization

#### File: `tail_cw/tui/trace_waterfall.py` (NEW)

```python
"""Waterfall timeline visualization for distributed traces.

Provides a Gantt-style chart showing span timelines, parent-child relationships,
and critical path highlighting.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from rich.console import Console, ConsoleOptions, RenderResult
from rich.segment import Segment
from rich.style import Style
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

    def render(self) -> RenderResult:
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
        yield result

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
        offset_chars = int((offset_ms / total_duration) * bar_width)

        duration = span.duration_ms or 0
        width_chars = max(1, int((duration / total_duration) * bar_width))

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
        offset_chars = int((offset_ms / total_duration) * bar_width)

        duration = span.duration_ms or 0
        width_chars = max(1, int((duration / total_duration) * bar_width))

        service = span.service_name[:12].ljust(12)
        bar = ' ' * offset_chars + '█' * width_chars
        duration_text = f"{duration:.1f}ms"

        error = "⚠" if span.is_error else " "
        line = f"{error} {service} |{bar[:bar_width]}| {duration_text}"
        lines.append(line)

    return "\n".join(lines)
```

**Tests to Add** (`tests/test_tui_trace_waterfall.py`):
- `test_waterfall_view_creation`
- `test_waterfall_service_colors`
- `test_waterfall_renders_spans`
- `test_waterfall_error_highlighting`
- `test_waterfall_ascii_export`

---

### Step 2: Implement Service Map View

#### File: `tail_cw/tui/service_map.py` (NEW)

```python
"""Service map visualization for distributed traces.

Shows service-to-service interactions inferred from trace spans.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from rich.table import Table
from rich.text import Text
from textual.widgets import Static

from tail_cw.query.trace import TraceGroup


@dataclass
class ServiceInteraction:
    """Represents an interaction between two services.

    Attributes:
        from_service: Source service name
        to_service: Destination service name
        call_count: Number of calls
        error_count: Number of failed calls
        avg_duration_ms: Average call duration
    """

    from_service: str
    to_service: str
    call_count: int
    error_count: int
    avg_duration_ms: float


def extract_service_interactions(traces: list[TraceGroup]) -> list[ServiceInteraction]:
    """Extract service-to-service interactions from traces.

    Analyzes parent-child span relationships to infer service calls.

    Args:
        traces: List of traces to analyze

    Returns:
        List of service interactions
    """
    interactions: dict[tuple[str, str], list[float]] = defaultdict(list)
    errors: dict[tuple[str, str], int] = defaultdict(int)

    for trace in traces:
        # Build parent-child mapping
        span_by_id = {span.span_id: span for span in trace.spans if span.span_id}

        for span in trace.spans:
            if span.parent_span_id and span.parent_span_id in span_by_id:
                parent = span_by_id[span.parent_span_id]
                from_service = parent.service_name
                to_service = span.service_name

                key = (from_service, to_service)
                if span.duration_ms:
                    interactions[key].append(span.duration_ms)

                if span.is_error:
                    errors[key] += 1

    # Convert to ServiceInteraction objects
    result = []
    for (from_service, to_service), durations in interactions.items():
        result.append(
            ServiceInteraction(
                from_service=from_service,
                to_service=to_service,
                call_count=len(durations),
                error_count=errors.get((from_service, to_service), 0),
                avg_duration_ms=sum(durations) / len(durations) if durations else 0,
            ),
        )

    return sorted(result, key=lambda x: x.call_count, reverse=True)


class ServiceMapView(Static):
    """Widget displaying service interaction map."""

    DEFAULT_CSS = """
    ServiceMapView {
        height: auto;
        border: solid $primary;
        padding: 1;
    }
    """

    def __init__(self, interactions: list[ServiceInteraction], *args, **kwargs) -> None:
        """Initialize service map view.

        Args:
            interactions: List of service interactions to display
            *args: Positional arguments for Static
            **kwargs: Keyword arguments for Static
        """
        super().__init__(*args, **kwargs)
        self._interactions = interactions

    def render(self):
        """Render service map as table.

        Returns:
            Renderable table
        """
        table = Table(title="Service Interaction Map", show_header=True, header_style="bold")

        table.add_column("From Service", style="cyan")
        table.add_column("To Service", style="magenta")
        table.add_column("Calls", justify="right")
        table.add_column("Errors", justify="right")
        table.add_column("Avg Duration", justify="right")
        table.add_column("Error Rate", justify="right")

        for interaction in self._interactions:
            error_rate = (
                (interaction.error_count / interaction.call_count) * 100
                if interaction.call_count > 0
                else 0
            )

            error_style = "red" if error_rate > 5 else "yellow" if error_rate > 0 else "green"

            table.add_row(
                interaction.from_service,
                interaction.to_service,
                str(interaction.call_count),
                Text(str(interaction.error_count), style=error_style),
                f"{interaction.avg_duration_ms:.2f}ms",
                Text(f"{error_rate:.1f}%", style=error_style),
            )

        yield table
```

**Tests to Add** (`tests/test_tui_service_map.py`):
- `test_extract_service_interactions`
- `test_service_interactions_with_errors`
- `test_service_map_view_creation`
- `test_service_map_table_rendering`

---

### Step 3: Add Jaeger Export

#### File: `tail_cw/export/__init__.py` (NEW)

```python
"""Export module for trace formats."""

from tail_cw.export.jaeger import export_trace_to_jaeger_json

__all__ = ['export_trace_to_jaeger_json']
```

#### File: `tail_cw/export/jaeger.py` (NEW)

```python
"""Jaeger JSON format export.

Exports traces in Jaeger JSON format for compatibility with Jaeger UI
and other OpenTelemetry tools.

Format specification: https://www.jaegertracing.io/docs/1.21/apis/#json
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tail_cw.query.trace import TraceGroup, TraceSpan


def export_trace_to_jaeger_json(trace: TraceGroup) -> dict[str, Any]:
    """Export a trace in Jaeger JSON format.

    Args:
        trace: TraceGroup to export

    Returns:
        Dictionary in Jaeger JSON format

    Example:
        >>> trace_json = export_trace_to_jaeger_json(trace)
        >>> with open('trace.json', 'w') as f:
        ...     json.dump(trace_json, f, indent=2)
    """
    spans = [_span_to_jaeger_format(span) for span in trace.spans]

    return {
        'data': [
            {
                'traceID': trace.trace_id,
                'spans': spans,
                'processes': _extract_processes(trace),
                'warnings': None,
            },
        ],
    }


def _span_to_jaeger_format(span: TraceSpan) -> dict[str, Any]:
    """Convert TraceSpan to Jaeger span format.

    Args:
        span: Span to convert

    Returns:
        Jaeger span dictionary
    """
    start_time_us = int(span.log_event.timestamp.timestamp() * 1_000_000)
    duration_us = int((span.duration_ms or 0) * 1000)

    tags = [
        {'key': 'service.name', 'type': 'string', 'value': span.service_name},
        {'key': 'error', 'type': 'bool', 'value': span.is_error},
    ]

    # Add custom tags from log message if JSONL
    if span.log_event.message.startswith('{'):
        try:
            import json

            msg_data = json.loads(span.log_event.message)
            for key, value in msg_data.items():
                if key not in ['trace_id', 'span_id', 'parent_span_id']:
                    tags.append({
                        'key': key,
                        'type': 'string',
                        'value': str(value),
                    })
        except json.JSONDecodeError:
            pass

    result = {
        'traceID': span.trace_id,
        'spanID': span.span_id or f'generated-{hash(span.log_event.event_id)}',
        'operationName': span.log_event.message[:100],  # Truncate
        'startTime': start_time_us,
        'duration': duration_us,
        'tags': tags,
        'logs': [
            {
                'timestamp': start_time_us,
                'fields': [
                    {'key': 'event', 'type': 'string', 'value': 'log'},
                    {'key': 'message', 'type': 'string', 'value': span.log_event.message},
                ],
            },
        ],
        'processID': f'p{hash(span.service_name) % 100}',
    }

    if span.parent_span_id:
        result['references'] = [
            {
                'refType': 'CHILD_OF',
                'traceID': span.trace_id,
                'spanID': span.parent_span_id,
            },
        ]

    return result


def _extract_processes(trace: TraceGroup) -> dict[str, Any]:
    """Extract process information from trace.

    Args:
        trace: TraceGroup

    Returns:
        Jaeger processes dictionary
    """
    processes = {}
    for service in trace.service_names:
        process_id = f'p{hash(service) % 100}'
        processes[process_id] = {
            'serviceName': service,
            'tags': [],
        }
    return processes


def export_traces_to_jaeger_file(traces: list[TraceGroup], output_path: Path) -> None:
    """Export multiple traces to Jaeger JSON file.

    Args:
        traces: List of traces to export
        output_path: Path to output JSON file

    Example:
        >>> export_traces_to_jaeger_file(traces, Path('traces.json'))
    """
    all_data = []
    for trace in traces:
        jaeger_trace = export_trace_to_jaeger_json(trace)
        all_data.extend(jaeger_trace['data'])

    output = {'data': all_data}

    with output_path.open('w') as f:
        json.dump(output, f, indent=2)
```

**Tests to Add** (`tests/test_export_jaeger.py`):
- `test_export_trace_to_jaeger_json`
- `test_jaeger_span_format`
- `test_jaeger_with_parent_references`
- `test_jaeger_error_tags`
- `test_export_multiple_traces_to_file`

---

### Step 4: Enhance Error Propagation Tracking

#### Update: `tail_cw/query/trace.py`

Add new functions:

```python
def analyze_error_propagation(trace: TraceGroup) -> dict[str, Any]:
    """Analyze how errors propagate through services in a trace.

    Args:
        trace: TraceGroup to analyze

    Returns:
        Dictionary with error propagation analysis:
        - root_error: First error span in the trace
        - affected_services: Services that encountered errors
        - propagation_path: List of services in error propagation order
        - error_correlation: Whether errors are related
    """
    error_spans = [span for span in trace.spans if span.is_error]

    if not error_spans:
        return {
            'has_errors': False,
            'root_error': None,
            'affected_services': set(),
            'propagation_path': [],
        }

    # Sort error spans by timestamp
    error_spans.sort(key=lambda s: s.log_event.timestamp)
    root_error = error_spans[0]

    affected_services = {span.service_name for span in error_spans}

    # Build propagation path using parent-child relationships
    propagation_path = _build_error_propagation_path(trace, root_error)

    return {
        'has_errors': True,
        'root_error': root_error,
        'affected_services': affected_services,
        'propagation_path': propagation_path,
        'total_errors': len(error_spans),
    }


def _build_error_propagation_path(trace: TraceGroup, root_error: TraceSpan) -> list[str]:
    """Build the path of error propagation through services.

    Args:
        trace: TraceGroup
        root_error: The first error span

    Returns:
        List of service names in propagation order
    """
    path = [root_error.service_name]

    # Build span lookup
    span_by_id = {span.span_id: span for span in trace.spans if span.span_id}

    # Find child error spans
    current_id = root_error.span_id
    while current_id:
        # Find children of current span that are errors
        children = [
            span
            for span in trace.spans
            if span.parent_span_id == current_id and span.is_error and span.service_name not in path
        ]

        if children:
            # Take the earliest error child
            child = min(children, key=lambda s: s.log_event.timestamp)
            path.append(child.service_name)
            current_id = child.span_id
        else:
            break

    return path
```

**Tests to Add** (`tests/test_query_trace.py` - append):
- `test_analyze_error_propagation_no_errors`
- `test_analyze_error_propagation_single_error`
- `test_analyze_error_propagation_multiple_services`
- `test_build_error_propagation_path`

---

### Step 5: Update TUI to Integrate New Features

#### Update: `tail_cw/tui/app.py`

Add new key bindings and views:

```python
# In bindings section:
BINDINGS = [
    # ... existing bindings ...
    ('w', 'toggle_waterfall', 'Waterfall'),
    ('m', 'toggle_service_map', 'Service Map'),
    ('j', 'export_jaeger', 'Export Jaeger'),
]

# Add new methods:
def action_toggle_waterfall(self) -> None:
    """Toggle waterfall timeline view for selected trace."""
    if not self._current_parquet_path:
        self._show_status('No data loaded')
        return

    # Get selected row trace ID
    row_key, _ = self._table.coordinate_to_cell_key(self._table.cursor_coordinate)
    row = self._table.get_row(row_key)
    # Extract trace ID from row...

    # Load trace
    trace = self._load_trace_by_id(trace_id)

    if trace:
        from tail_cw.tui.trace_waterfall import TraceWaterfallView
        waterfall = TraceWaterfallView(trace)
        # Show in modal or panel
        self.push_screen(waterfall)


def action_toggle_service_map(self) -> None:
    """Show service map for all loaded traces."""
    # Load all traces
    traces = self._load_all_traces()

    from tail_cw.tui.service_map import extract_service_interactions, ServiceMapView
    interactions = extract_service_interactions(traces)
    map_view = ServiceMapView(interactions)

    self.push_screen(map_view)


def action_export_jaeger(self) -> None:
    """Export current traces to Jaeger JSON format."""
    from pathlib import Path
    from tail_cw.export.jaeger import export_traces_to_jaeger_file

    traces = self._load_all_traces()
    output_path = Path('traces_export.json')

    export_traces_to_jaeger_file(traces, output_path)
    self._show_status(f'Exported {len(traces)} traces to {output_path}')
```

---

### Step 6: Documentation Updates

#### Update: `docs/docs/CONFIGURATION.md`

Add new section:

```markdown
## Distributed Tracing Features

tail-cw includes advanced distributed tracing capabilities:

### Waterfall Timeline

View span timelines in a Gantt-style chart:
- Press `w` on a trace to open waterfall view
- Spans are color-coded by service
- Error spans highlighted in red
- Critical path visualization

### Service Map

Visualize service-to-service interactions:
- Press `m` to open service map
- Shows call counts, error rates, and average durations
- Helps identify bottlenecks and failure points

### Jaeger Export

Export traces for use with Jaeger UI:
- Press `j` to export all loaded traces
- Creates `traces_export.json` in Jaeger format
- Import into Jaeger for advanced visualization

### Error Propagation

Automatic error propagation analysis:
- Identifies root cause of errors
- Tracks error spread through services
- Shows affected services and propagation path
```

---

## Testing Strategy

### Unit Tests

Create test files:
- `tests/test_tui_trace_waterfall.py` (15+ tests)
- `tests/test_tui_service_map.py` (10+ tests)
- `tests/test_export_jaeger.py` (10+ tests)
- Update `tests/test_query_trace.py` (add 5+ tests)

### Integration Tests

- `tests/test_tui_app_tracing_integration.py` - Test full workflow
- Verify waterfall renders correctly
- Verify service map with multiple traces
- Verify Jaeger export format validity

### Manual Testing Checklist

- [ ] Load traces and open waterfall view (press `w`)
- [ ] Verify spans render with correct timing
- [ ] Verify error spans highlighted
- [ ] Open service map (press `m`)
- [ ] Verify interactions shown correctly
- [ ] Export to Jaeger (press `j`)
- [ ] Import exported file into Jaeger UI
- [ ] Verify error propagation analysis

---

## Implementation Timeline

**Estimated Time:** 8-12 hours

1. **Waterfall View** (3-4 hours)
2. **Service Map** (2-3 hours)
3. **Jaeger Export** (2 hours)
4. **Error Propagation** (1-2 hours)
5. **TUI Integration** (1-2 hours)
6. **Tests** (2-3 hours)
7. **Documentation** (1 hour)

---

## Success Criteria

- [ ] Waterfall timeline renders correctly for sample traces
- [ ] Service map shows interactions with error rates
- [ ] Jaeger export produces valid JSON importable by Jaeger
- [ ] Error propagation correctly identifies root causes
- [ ] All new tests passing (35+ tests)
- [ ] Documentation updated with examples
- [ ] No regressions in existing functionality

---

## Notes

- **Jaeger Format:** Follow Jaeger JSON v1 specification
- **Performance:** Waterfall rendering should handle 100+ spans smoothly
- **Color Palette:** Use consistent colors across views
- **Accessibility:** Ensure error indicators work in monochrome terminals
- **Export Path:** Make configurable via CLI argument in future

---

## Commands to Run

```bash
# Create branch
git checkout -b claude/distributed-tracing-enhancements-01VGBEGD5jDcQmxrbdNnW9zA

# Create files and implement
# ... implement all files above ...

# Run tests
uv run pytest tests/test_tui_trace_waterfall.py -v
uv run pytest tests/test_tui_service_map.py -v
uv run pytest tests/test_export_jaeger.py -v
uv run pytest tests/test_query_trace.py -v

# Run all checks
uv run ruff format
uv run ruff check --fix --unsafe-fixes
uv run mypy tail_cw tests
uv run pytest -q --ff

# Commit and push
git add -A
git commit -m "feat: add distributed tracing enhancements (Branch 2)

- Added waterfall timeline visualization with Gantt-style charts
- Implemented service map view showing service interactions
- Added Jaeger JSON export for compatibility with Jaeger UI
- Enhanced error propagation tracking and analysis
- Integrated new views into TUI with keyboard shortcuts (w, m, j)
- Added 35+ tests for new functionality
- Updated documentation with tracing feature guide

Implements 'Focus on Distributed Tracing' strategy from PROJECT_REVIEW.md"

git push -u origin claude/distributed-tracing-enhancements-01VGBEGD5jDcQmxrbdNnW9zA
```
