"""Trace extraction and grouping for distributed tracing support.

This module provides functionality to extract trace IDs from log events,
group events by trace, and create trace groups for visualization.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, TypedDict

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

ERROR_KEYWORDS = {'error', 'fatal', 'critical', 'exception'}
ERROR_LEVEL_FIELDS = {'level', 'severity', 'loglevel'}
STATUS_FIELDS = {'status', 'status_code', 'statuscode'}
MESSAGE_FIELDS = {'message', 'msg', 'error_message'}
ERROR_STATUS_THRESHOLD = 500
SPAN_ID_FIELDS = ['span_id', 'spanId', 'id']
PARENT_SPAN_FIELDS = ['parent_span_id', 'parentSpanId', 'parent_id']
DURATION_FIELDS = ['duration', 'duration_ms', 'elapsed_ms', 'durationMs']
MILLISECONDS_PER_SECOND = 1000
MILLISECONDS_PER_MINUTE = 60 * MILLISECONDS_PER_SECOND


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


class SpanMetadata(TypedDict):
    """Structured metadata extracted from a log event."""

    span_id: str | None
    parent_span_id: str | None
    duration_ms: float | None


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
    for data in _iter_structured_event_data(event):
        trace_id = _search_for_trace_id(data, trace_id_fields)
        if trace_id:
            return trace_id

    return None


def _load_json_dict(payload: str | None) -> dict[str, Any] | None:
    """Safely load a JSON dict from payload.

    Returns:
        Parsed dict when payload is valid JSON, otherwise None.
    """
    if not payload:
        return None

    with contextlib.suppress(json.JSONDecodeError, TypeError):
        parsed = json.loads(payload)
        if isinstance(parsed, dict):
            return parsed
    return None


def _iter_structured_event_data(event: LogEvent) -> Iterator[dict[str, Any]]:
    """Yield structured representations of a log event."""
    message_data = _load_json_dict(event.message)
    if message_data:
        yield message_data

    parsed_attr = getattr(event, 'parsed', None)
    if isinstance(parsed_attr, dict):
        yield parsed_attr


def _search_for_trace_id(data: Mapping[str, Any], field_names: Iterable[str]) -> str | None:
    """Search for trace ID in a dict using field names.

    Supports nested paths like 'context.trace_id'.

    Args:
        data: Dict to search in.
        field_names: Field names to search for.

    Returns:
        Trace ID string or None.
    """
    for field_name in field_names:
        value = _resolve_field_path(data, field_name.split('.'))
        if value:
            return str(value)
    return None


def _resolve_field_path(data: Mapping[str, Any], path: list[str]) -> Any | None:
    """Resolve a dotted field path using case-insensitive keys.

    Returns:
        Resolved value or None when any path segment is missing.
    """
    current: Any = data
    for part in path:
        if not isinstance(current, Mapping):
            return None
        current = _get_case_insensitive(current, part)
        if current is None:
            return None
    return current


def _get_case_insensitive(data: Mapping[str, Any], key: str) -> Any | None:
    """Retrieve a value by key using case-insensitive lookup.

    Returns:
        Matching value, or None if no case-insensitive match exists.
    """
    lowered = key.lower()
    for current_key, value in data.items():
        if current_key.lower() == lowered:
            return value
    return None


def _find_first_matching_field(data: Mapping[str, Any], field_names: Iterable[str]) -> str | None:
    """Find the first value that matches any field name case-insensitively.

    Returns:
        Stringified value when a match is found, otherwise None.
    """
    for field_name in field_names:
        value = _get_case_insensitive(data, field_name)
        if value:
            return str(value)
    return None


def _coerce_to_float(value: Any) -> float | None:
    """Convert value to float when possible.

    Returns:
        Float representation or None when conversion fails.
    """
    with contextlib.suppress(ValueError, TypeError):
        return float(value)
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

    for data in _iter_structured_event_data(event):
        value = _find_first_matching_field(data, service_fields)
        if value:
            return value

    # Fall back to log_group, strip common prefixes
    log_group = event.log_group
    for prefix in ['/aws/lambda/', '/aws/', '/ecs/']:
        if log_group.startswith(prefix):
            log_group = log_group[len(prefix) :]
            break
    return log_group


def _message_contains_error_keyword(message: str) -> bool:
    """Return True if the message contains an error keyword."""
    message_lower = message.lower()
    return any(keyword in message_lower for keyword in ERROR_KEYWORDS)


def _structured_data_indicates_error(data: Mapping[str, Any]) -> bool:
    """Inspect structured data for error indicators.

    Returns:
        True when error semantics are detected, otherwise False.
    """
    for key, value in data.items():
        if not value:
            continue

        lowered_key = key.lower()
        if lowered_key in ERROR_LEVEL_FIELDS and str(value).upper() in {'ERROR', 'FATAL', 'CRITICAL'}:
            return True

        if lowered_key in STATUS_FIELDS:
            with contextlib.suppress(ValueError, TypeError):
                if int(value) >= ERROR_STATUS_THRESHOLD:
                    return True
    return False


def _structured_message_contains_error(data: Mapping[str, Any]) -> bool:
    """Inspect structured message fields for error keywords.

    Returns:
        True when message-like fields contain error terms, otherwise False.
    """
    for key, value in data.items():
        if key.lower() in MESSAGE_FIELDS and isinstance(value, str) and _message_contains_error_keyword(value):
            return True
    return False


def is_error_event(event: LogEvent) -> bool:
    """Determine if an event represents an error.

    Checks message for error keywords and parsed JSON for level/status fields.

    Args:
        event: The log event.

    Returns:
        True if event is an error.
    """
    structured_found = False

    for data in _iter_structured_event_data(event):
        structured_found = True
        if _structured_data_indicates_error(data) or _structured_message_contains_error(data):
            return True

    if structured_found:
        return False

    return _message_contains_error_keyword(event.message)


def extract_span_metadata(event: LogEvent) -> SpanMetadata:
    """Extract span metadata from LogEvent.

    Args:
        event: The log event.

    Returns:
        Dict with span_id, parent_span_id, duration_ms (None for missing fields).
    """
    metadata: SpanMetadata = {
        'span_id': None,
        'parent_span_id': None,
        'duration_ms': None,
    }

    for data in _iter_structured_event_data(event):
        if metadata['span_id'] is None:
            span_value = _find_first_matching_field(data, SPAN_ID_FIELDS)
            if span_value:
                metadata['span_id'] = span_value

        if metadata['parent_span_id'] is None:
            parent_value = _find_first_matching_field(data, PARENT_SPAN_FIELDS)
            if parent_value:
                metadata['parent_span_id'] = parent_value

        if metadata['duration_ms'] is None:
            duration_value = _find_first_matching_field(data, DURATION_FIELDS)
            if duration_value is not None:
                duration = _coerce_to_float(duration_value)
                if duration is not None:
                    metadata['duration_ms'] = duration

        if metadata['span_id'] and metadata['parent_span_id'] and metadata['duration_ms'] is not None:
            break

    return metadata


def log_event_to_trace_span(
    event: LogEvent,
    trace_id: str,
) -> TraceSpan:
    """Convert LogEvent to TraceSpan.

    Args:
        event: The log event.
        trace_id: The trace ID (already extracted).

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
            span = log_event_to_trace_span(event, trace_id)
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


def format_trace_duration(duration_ms: float | int) -> str:  # noqa: PYI041
    """Format duration for display.

    Args:
        duration_ms: Duration in milliseconds (int or float).

    Returns:
        Formatted duration (e.g., '1.23s', '456ms', '1m 23s').
    """
    duration_value = float(duration_ms)

    if duration_value < MILLISECONDS_PER_SECOND:
        return f'{int(duration_value)}ms'
    if duration_value < MILLISECONDS_PER_MINUTE:
        return f'{duration_value / MILLISECONDS_PER_SECOND:.2f}s'
    minutes = int(duration_value / MILLISECONDS_PER_MINUTE)
    seconds = int((duration_value % MILLISECONDS_PER_MINUTE) / MILLISECONDS_PER_SECOND)
    return f'{minutes}m {seconds}s'
