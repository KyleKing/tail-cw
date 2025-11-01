"""Trace extraction and grouping for distributed tracing support.

This module provides functionality to extract trace IDs from log events,
group events by trace, and create trace groups for visualization.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from tail_cw.aws.client import LogEvent
from tail_cw.query.engine import query_parquet_file_to_log_events

DEFAULT_TRACE_ID_FIELDS = [
    'trace_id',
    'traceId',
    'trace-id',
    'x-trace-id',
    'x_trace_id',
    'TraceId',
    'request_id',
    'requestId',
]


@dataclass(frozen=True)
class TraceSpan:
    """Represents a span in a distributed trace.

    Attributes:
        log_event: The underlying log event.
        trace_id: Extracted trace ID.
        span_id: Optional span ID if available.
        parent_span_id: Optional parent span ID for hierarchy.
        service_name: Service/source name (derived from log_group or parsed field).
        duration_ms: Optional duration in milliseconds.
        is_error: Whether this span represents an error.
    """

    log_event: LogEvent
    trace_id: str
    span_id: str | None
    parent_span_id: str | None
    service_name: str
    duration_ms: float | None
    is_error: bool


@dataclass(frozen=True)
class TraceGroup:
    """Represents a grouped trace with all its spans.

    Attributes:
        trace_id: The trace ID.
        spans: All spans in this trace, sorted chronologically.
        start_time: Earliest timestamp in the trace.
        end_time: Latest timestamp in the trace.
        duration_ms: Total trace duration in milliseconds.
        service_names: Unique services involved in this trace.
        error_count: Number of error spans.
        span_count: Total number of spans.
    """

    trace_id: str
    spans: list[TraceSpan]
    start_time: datetime
    end_time: datetime
    duration_ms: float
    service_names: set[str]
    error_count: int
    span_count: int


def extract_trace_id_from_event(
    event: LogEvent,
    trace_id_fields: list[str] = DEFAULT_TRACE_ID_FIELDS,
) -> str | None:
    """Extract trace ID from a LogEvent.

    Searches for trace ID in the event's message (if JSONL) and in any
    'parsed' attribute from Parquet. Supports nested field paths like
    'context.trace_id'.

    Args:
        event: The log event to extract from.
        trace_id_fields: Field names to search (case-insensitive).

    Returns:
        Extracted trace ID or None if not found.

    Examples:
        >>> event = LogEvent(message='{"trace_id": "abc123"}', ...)
        >>> extract_trace_id_from_event(event)
        'abc123'

        >>> event = LogEvent(message='{"context": {"trace_id": "xyz"}}', ...)
        >>> extract_trace_id_from_event(event)
        'xyz'
    """
    # Try to parse message as JSON
    parsed_data = None
    with contextlib.suppress(json.JSONDecodeError, TypeError):
        parsed_data = json.loads(event.message)

    # Search in parsed message
    if parsed_data and isinstance(parsed_data, dict):
        trace_id = _search_for_trace_id(parsed_data, trace_id_fields)
        if trace_id:
            return trace_id

    # Check if event has 'parsed' attribute from Parquet
    if hasattr(event, 'parsed') and isinstance(event.parsed, dict):
        trace_id = _search_for_trace_id(event.parsed, trace_id_fields)
        if trace_id:
            return trace_id

    return None


def _search_for_trace_id(data: dict, field_names: list[str]) -> str | None:
    """Search for trace ID in a dict using field names.

    Supports nested paths like 'context.trace_id'.

    Args:
        data: Dict to search in.
        field_names: Field names to search for.

    Returns:
        Trace ID string or None.
    """
    # Try direct field lookup (case-insensitive)
    for field_name in field_names:
        for key, value in data.items():
            if key.lower() == field_name.lower() and value:
                return str(value)

    # Try nested paths
    for field_name in field_names:
        if '.' in field_name:
            parts = field_name.split('.')
            current = data
            for part in parts:
                if isinstance(current, dict):
                    # Case-insensitive lookup
                    found = False
                    for key, value in current.items():
                        if key.lower() == part.lower():
                            current = value
                            found = True
                            break
                    if not found:
                        break
                else:
                    break
            else:
                if current:
                    return str(current)

    return None


def extract_service_name(event: LogEvent) -> str:
    """Extract service name from LogEvent.

    Tries to extract from parsed JSON fields like 'service_name', 'service',
    'serviceName', 'app', 'application'. Falls back to log_group name.

    Args:
        event: The log event.

    Returns:
        Service name (defaults to log_group if not found).
    """
    service_fields = ['service_name', 'service', 'serviceName', 'app', 'application']

    # Try parsing message as JSON
    try:
        parsed_data = json.loads(event.message)
        if isinstance(parsed_data, dict):
            for field in service_fields:
                for key, value in parsed_data.items():
                    if key.lower() == field.lower() and value:
                        return str(value)
    except (json.JSONDecodeError, TypeError):
        pass

    # Check parsed attribute from Parquet
    if hasattr(event, 'parsed') and isinstance(event.parsed, dict):
        for field in service_fields:
            for key, value in event.parsed.items():
                if key.lower() == field.lower() and value:
                    return str(value)

    # Fall back to log_group, strip common prefixes
    log_group = event.log_group
    for prefix in ['/aws/lambda/', '/aws/', '/ecs/']:
        if log_group.startswith(prefix):
            log_group = log_group[len(prefix) :]
            break
    return log_group


def is_error_event(event: LogEvent) -> bool:
    """Determine if an event represents an error.

    Checks message for error keywords and parsed JSON for level/status fields.

    Args:
        event: The log event.

    Returns:
        True if event is an error.
    """
    # Check message for error keywords
    message_lower = event.message.lower()
    error_keywords = ['error', 'fatal', 'critical', 'exception']
    for keyword in error_keywords:
        if keyword in message_lower:
            return True

    # Try parsing message as JSON
    try:
        parsed_data = json.loads(event.message)
        if isinstance(parsed_data, dict):
            # Check level field
            for key, value in parsed_data.items():
                if key.lower() in {'level', 'severity', 'loglevel'} and value:
                    if str(value).upper() in {'ERROR', 'FATAL', 'CRITICAL'}:
                        return True
                # Check status code
                if key.lower() in {'status', 'status_code', 'statuscode'} and value:
                    try:
                        if int(value) >= 500:
                            return True
                    except (ValueError, TypeError):
                        pass
    except (json.JSONDecodeError, TypeError):
        pass

    # Check parsed attribute from Parquet
    if hasattr(event, 'parsed') and isinstance(event.parsed, dict):
        for key, value in event.parsed.items():
            if key.lower() in {'level', 'severity', 'loglevel'} and value:
                if str(value).upper() in {'ERROR', 'FATAL', 'CRITICAL'}:
                    return True
            if key.lower() in {'status', 'status_code', 'statuscode'} and value:
                try:
                    if int(value) >= 500:
                        return True
                except (ValueError, TypeError):
                    pass

    return False


def extract_span_metadata(event: LogEvent) -> dict:
    """Extract span metadata from LogEvent.

    Args:
        event: The log event.

    Returns:
        Dict with span_id, parent_span_id, duration_ms (None for missing fields).
    """
    metadata = {
        'span_id': None,
        'parent_span_id': None,
        'duration_ms': None,
    }

    span_id_fields = ['span_id', 'spanId', 'id']
    parent_fields = ['parent_span_id', 'parentSpanId', 'parent_id']
    duration_fields = ['duration', 'duration_ms', 'elapsed_ms', 'durationMs']

    # Try parsing message as JSON
    parsed_data = None
    with contextlib.suppress(json.JSONDecodeError, TypeError):
        parsed_data = json.loads(event.message)

    if parsed_data and isinstance(parsed_data, dict):
        # Extract span_id
        for field in span_id_fields:
            for key, value in parsed_data.items():
                if key.lower() == field.lower() and value:
                    metadata['span_id'] = str(value)
                    break
            if metadata['span_id']:
                break

        # Extract parent_span_id
        for field in parent_fields:
            for key, value in parsed_data.items():
                if key.lower() == field.lower() and value:
                    metadata['parent_span_id'] = str(value)
                    break
            if metadata['parent_span_id']:
                break

        # Extract duration
        for field in duration_fields:
            for key, value in parsed_data.items():
                if key.lower() == field.lower() and value:
                    with contextlib.suppress(ValueError, TypeError):
                        metadata['duration_ms'] = float(value)
                    break
            if metadata['duration_ms']:
                break

    # Check parsed attribute from Parquet
    if hasattr(event, 'parsed') and isinstance(event.parsed, dict):
        if not metadata['span_id']:
            for field in span_id_fields:
                for key, value in event.parsed.items():
                    if key.lower() == field.lower() and value:
                        metadata['span_id'] = str(value)
                        break
                if metadata['span_id']:
                    break

        if not metadata['parent_span_id']:
            for field in parent_fields:
                for key, value in event.parsed.items():
                    if key.lower() == field.lower() and value:
                        metadata['parent_span_id'] = str(value)
                        break
                if metadata['parent_span_id']:
                    break

        if not metadata['duration_ms']:
            for field in duration_fields:
                for key, value in event.parsed.items():
                    if key.lower() == field.lower() and value:
                        with contextlib.suppress(ValueError, TypeError):
                            metadata['duration_ms'] = float(value)
                        break
                if metadata['duration_ms']:
                    break

    return metadata


def log_event_to_trace_span(
    event: LogEvent,
    trace_id: str,
    trace_id_fields: list[str] = DEFAULT_TRACE_ID_FIELDS,
) -> TraceSpan:
    """Convert LogEvent to TraceSpan.

    Args:
        event: The log event.
        trace_id: The trace ID (already extracted).
        trace_id_fields: Field names for extraction.

    Returns:
        Constructed TraceSpan instance.
    """
    service_name = extract_service_name(event)
    is_error = is_error_event(event)
    metadata = extract_span_metadata(event)

    return TraceSpan(
        log_event=event,
        trace_id=trace_id,
        span_id=metadata['span_id'],
        parent_span_id=metadata['parent_span_id'],
        service_name=service_name,
        duration_ms=metadata['duration_ms'],
        is_error=is_error,
    )


def group_events_by_trace(
    events: Iterable[LogEvent],
    trace_id_fields: list[str] = DEFAULT_TRACE_ID_FIELDS,
) -> dict[str, list[TraceSpan]]:
    """Group LogEvents by trace ID.

    Args:
        events: Iterator of log events.
        trace_id_fields: Field names to search.

    Returns:
        Dict mapping trace_id to list of TraceSpans.
    """
    grouped: dict[str, list[TraceSpan]] = {}

    for event in events:
        trace_id = extract_trace_id_from_event(event, trace_id_fields)
        if trace_id:
            span = log_event_to_trace_span(event, trace_id, trace_id_fields)
            if trace_id not in grouped:
                grouped[trace_id] = []
            grouped[trace_id].append(span)

    return grouped


def create_trace_groups(grouped_spans: dict[str, list[TraceSpan]]) -> list[TraceGroup]:
    """Create TraceGroup instances from grouped spans.

    Args:
        grouped_spans: Grouped spans by trace ID.

    Returns:
        List of TraceGroup instances, sorted by start_time (most recent first).
    """
    trace_groups = []

    for trace_id, spans in grouped_spans.items():
        # Sort spans chronologically
        sorted_spans = sorted(spans, key=lambda s: s.log_event.timestamp)

        # Calculate metadata
        start_time = sorted_spans[0].log_event.timestamp
        end_time = sorted_spans[-1].log_event.timestamp
        duration_ms = (end_time - start_time).total_seconds() * 1000

        service_names = {span.service_name for span in sorted_spans}
        error_count = sum(1 for span in sorted_spans if span.is_error)
        span_count = len(sorted_spans)

        trace_group = TraceGroup(
            trace_id=trace_id,
            spans=sorted_spans,
            start_time=start_time,
            end_time=end_time,
            duration_ms=duration_ms,
            service_names=service_names,
            error_count=error_count,
            span_count=span_count,
        )
        trace_groups.append(trace_group)

    # Sort by start_time, most recent first
    trace_groups.sort(key=lambda tg: tg.start_time, reverse=True)

    return trace_groups


def query_traces_from_parquet(
    parquet_path: Path,
    trace_id: str | None = None,
    trace_id_fields: list[str] = DEFAULT_TRACE_ID_FIELDS,
    limit: int | None = None,
) -> list[TraceGroup]:
    """Query Parquet file for traces.

    Args:
        parquet_path: Path to Parquet file.
        trace_id: Optional specific trace ID to filter.
        trace_id_fields: Field names to search.
        limit: Limit number of traces returned.

    Returns:
        List of TraceGroup instances.

    Examples:
        >>> traces = query_traces_from_parquet(Path('logs.parquet'))
        >>> len(traces)
        5

        >>> traces = query_traces_from_parquet(
        ...     Path('logs.parquet'),
        ...     trace_id='abc123'
        ... )
        >>> traces[0].trace_id
        'abc123'
    """
    # Query all events from Parquet file
    try:
        events = list(query_parquet_file_to_log_events(parquet_path, None))
    except Exception:
        # If query fails, return empty list
        return []

    # Group events by trace
    grouped_spans = group_events_by_trace(events, trace_id_fields)

    # Create trace groups
    trace_groups = create_trace_groups(grouped_spans)

    # Filter by specific trace_id if requested
    if trace_id:
        trace_groups = [tg for tg in trace_groups if tg.trace_id == trace_id]

    # Apply limit
    if limit:
        trace_groups = trace_groups[:limit]

    return trace_groups


def find_traces_with_errors(
    parquet_path: Path,
    trace_id_fields: list[str] = DEFAULT_TRACE_ID_FIELDS,
) -> list[TraceGroup]:
    """Find all traces containing errors.

    Args:
        parquet_path: Path to Parquet file.
        trace_id_fields: Field names.

    Returns:
        List of TraceGroups with errors, sorted by error_count descending.
    """
    # Query for all events (we need to check each for errors)
    try:
        events = query_parquet_file_to_log_events(parquet_path, None)
    except Exception:
        return []

    # Group by trace
    grouped_spans = group_events_by_trace(events, trace_id_fields)

    # Create trace groups
    trace_groups = create_trace_groups(grouped_spans)

    # Filter to only traces with errors
    error_traces = [tg for tg in trace_groups if tg.error_count > 0]

    # Sort by error_count descending
    error_traces.sort(key=lambda tg: tg.error_count, reverse=True)

    return error_traces


def format_trace_duration(duration_ms: float) -> str:
    """Format duration for display.

    Args:
        duration_ms: Duration in milliseconds.

    Returns:
        Formatted duration (e.g., '1.23s', '456ms', '1m 23s').
    """
    if duration_ms < 1000:
        return f'{int(duration_ms)}ms'
    if duration_ms < 60000:
        return f'{duration_ms / 1000:.2f}s'
    minutes = int(duration_ms / 60000)
    seconds = int((duration_ms % 60000) / 1000)
    return f'{minutes}m {seconds}s'
