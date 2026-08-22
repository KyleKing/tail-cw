"""Serialize grouped log events as OTLP JSON, for viewers that already draw traces.

Per [ADR 0012](../../docs/docs/adr/0012-export-traces-instead-of-drawing-them.md)
tail-cw emits a standard document rather than drawing a waterfall. The honest
mapping from a log line to a span is the constraint here: a line carries one
timestamp, and services log when work finishes, so a span covers
``[timestamp - duration, timestamp]`` when the record names a duration and is
instantaneous when it does not.

Identity follows the same rule. Every line a service logs inside one span
carries that span's id, so those lines are the span's events rather than 72
spans sharing an id, which is what a viewer would otherwise show as one span
repeated. A line carrying no span id at all becomes a span of its own.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from hashlib import blake2b
from typing import Any

from tail_cw.aws.events import LogEvent
from tail_cw.query.severity import load_json_dict
from tail_cw.query.trace import TraceGroup, TraceSpan

SCOPE_NAME = 'tail-cw'
STATUS_CODE_ERROR = 2
STATUS_CODE_UNSET = 0
_TRACE_ID_HEX = 32
_SPAN_ID_HEX = 16
_NANOS_PER_MS = 1_000_000
_ATTRIBUTE_SKIP = frozenset({'duration', 'duration_ms', 'durationMs', 'elapsed_ms', 'parent_span_id', 'span_id'})


def trace_groups_to_otlp(groups: Sequence[TraceGroup]) -> dict[str, Any]:
    """Build one OTLP JSON document covering every span in ``groups``.

    Spans are grouped into one ``resourceSpans`` entry per service, which is
    what a viewer reads as the swimlane.
    """
    by_service: dict[str, list[list[TraceSpan]]] = {}
    for group in groups:
        for lines in _cluster_by_span(group.spans):
            by_service.setdefault(lines[0].service_name, []).append(lines)
    return {
        'resourceSpans': [
            {
                'resource': {'attributes': [_attribute('service.name', service)]},
                'scopeSpans': [{'scope': {'name': SCOPE_NAME}, 'spans': [_span(lines) for lines in clusters]}],
            }
            for service, clusters in by_service.items()
        ],
    }


def _cluster_by_span(spans: Sequence[TraceSpan]) -> list[list[TraceSpan]]:
    """Group the lines that share one span id, keeping id-less lines separate."""
    clustered: dict[tuple[str, str], list[TraceSpan]] = {}
    singles: list[list[TraceSpan]] = []
    for span in spans:
        if span.span_id:
            clustered.setdefault((span.service_name, span.span_id), []).append(span)
        else:
            singles.append([span])
    return [*clustered.values(), *singles]


def trace_error_summary(group: TraceGroup) -> str:
    """One line naming the trace's shape: services touched, errors, first to fail."""
    first_error = next((span for span in group.spans if span.is_error), None)
    blame = f'first error in {first_error.service_name}' if first_error is not None else 'no errors'
    return (
        f'{group.trace_id}: {group.span_count} spans across {len(group.service_names)} services '
        f'over {group.duration_ms:.0f}ms, {group.error_count} errors, {blame}'
    )


def _span(lines: Sequence[TraceSpan]) -> dict[str, Any]:
    """Render one span from the lines logged inside it, longest work first."""
    lead = max(lines, key=lambda line: line.duration_ms or 0.0)
    body = load_json_dict(lead.log_event.message) or {}
    intervals = [_interval(line) for line in lines]
    rendered = {
        'traceId': _hex_id(lead.trace_id, width=_TRACE_ID_HEX),
        'spanId': _span_id(lead),
        'name': _span_name(body, lead),
        'startTimeUnixNano': str(min(start for start, _ in intervals)),
        'endTimeUnixNano': str(max(end for _, end in intervals)),
        'attributes': list(_attributes(body, lead.log_event)),
        'status': {'code': STATUS_CODE_ERROR if any(line.is_error for line in lines) else STATUS_CODE_UNSET},
    }
    if lead.parent_span_id:
        rendered['parentSpanId'] = _hex_id(lead.parent_span_id, width=_SPAN_ID_HEX)
    if len(lines) > 1:
        rendered['events'] = [_event(line) for line in lines]
    return rendered


def _interval(span: TraceSpan) -> tuple[int, int]:
    end_nanos = int(span.log_event.timestamp.timestamp() * 1_000_000_000)
    return end_nanos - int((span.duration_ms or 0.0) * _NANOS_PER_MS), end_nanos


def _event(span: TraceSpan) -> dict[str, Any]:
    body = load_json_dict(span.log_event.message) or {}
    return {
        'name': _span_name(body, span),
        'timeUnixNano': str(int(span.log_event.timestamp.timestamp() * 1_000_000_000)),
        'attributes': list(_attributes(body, span.log_event)),
    }


def _span_name(body: Mapping[str, Any], span: TraceSpan) -> str:
    for key in ('event', 'operation', 'name', 'message', 'msg'):
        value = body.get(key)
        if isinstance(value, str) and value:
            return value
    return span.service_name


def _attributes(body: Mapping[str, Any], event: LogEvent) -> Iterable[dict[str, Any]]:
    yield _attribute('aws.log_group', event.log_group)
    yield _attribute('aws.log_stream', event.log_stream)
    for key, value in body.items():
        if key not in _ATTRIBUTE_SKIP and value is not None and not isinstance(value, (dict, list)):
            yield _attribute(key, value)


def _attribute(key: str, value: Any) -> dict[str, Any]:
    match value:
        case bool():
            return {'key': key, 'value': {'boolValue': value}}
        case int():
            return {'key': key, 'value': {'intValue': str(value)}}
        case float():
            return {'key': key, 'value': {'doubleValue': value}}
        case _:
            return {'key': key, 'value': {'stringValue': str(value)}}


def _span_id(span: TraceSpan) -> str:
    if span.span_id:
        return _hex_id(span.span_id, width=_SPAN_ID_HEX)
    # OTLP rejects a span with no id, and our logs often carry none. Deriving it
    # from the line keeps two exports of the same event identical.
    event = span.log_event
    seed = f'{event.log_group}|{event.log_stream}|{event.timestamp.isoformat()}|{event.message}'
    return blake2b(seed.encode(), digest_size=_SPAN_ID_HEX // 2).hexdigest()


def _hex_id(value: str, *, width: int) -> str:
    """Coerce an identifier to the fixed-width hex OTLP requires.

    An X-Ray id is ``1-68a1f2c3-4d5e…``: a format version, then 32 hex digits of
    identity. Keeping the version would shift every digit and name a different
    trace, so it is dropped. Anything non-hex is hashed rather than mangled.
    """
    segments = value.lower().split('-')
    if len(segments) > 1 and len(segments[0]) == 1:
        segments = segments[1:]
    hexed = ''.join(char for char in ''.join(segments) if char in '0123456789abcdef')
    if len(hexed) >= width:
        return hexed[:width]
    if hexed and len(value.replace('-', '')) == len(hexed):
        return hexed.rjust(width, '0')
    return blake2b(value.encode(), digest_size=width // 2).hexdigest()
