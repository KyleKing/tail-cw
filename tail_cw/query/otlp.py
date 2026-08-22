"""Serialize grouped log events as OTLP JSON, for viewers that already draw traces.

Per [ADR 0012](../../docs/docs/adr/0012-export-traces-instead-of-drawing-them.md)
tail-cw emits a standard document rather than drawing a waterfall. The honest
mapping from a log line to a span is the constraint here: a line carries one
timestamp, and services log when work finishes, so a span covers
``[timestamp - duration, timestamp]`` when the record names a duration and is
instantaneous when it does not.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from hashlib import blake2b
from typing import Any

from tail_cw.aws.client import LogEvent
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
    by_service: dict[str, list[TraceSpan]] = {}
    for group in groups:
        for span in group.spans:
            by_service.setdefault(span.service_name, []).append(span)
    return {
        'resourceSpans': [
            {
                'resource': {'attributes': [_attribute('service.name', service)]},
                'scopeSpans': [{'scope': {'name': SCOPE_NAME}, 'spans': [_span(span) for span in spans]}],
            }
            for service, spans in by_service.items()
        ],
    }


def trace_error_summary(group: TraceGroup) -> str:
    """One line naming the trace's shape: services touched, errors, first to fail."""
    first_error = next((span for span in group.spans if span.is_error), None)
    blame = f'first error in {first_error.service_name}' if first_error is not None else 'no errors'
    return (
        f'{group.trace_id}: {group.span_count} spans across {len(group.service_names)} services '
        f'over {group.duration_ms:.0f}ms, {group.error_count} errors, {blame}'
    )


def _span(span: TraceSpan) -> dict[str, Any]:
    end_nanos = int(span.log_event.timestamp.timestamp() * 1_000_000_000)
    duration_nanos = int((span.duration_ms or 0.0) * _NANOS_PER_MS)
    body = load_json_dict(span.log_event.message) or {}
    rendered = {
        'traceId': _hex_id(span.trace_id, width=_TRACE_ID_HEX),
        'spanId': _span_id(span),
        'name': _span_name(body, span),
        'startTimeUnixNano': str(end_nanos - duration_nanos),
        'endTimeUnixNano': str(end_nanos),
        'attributes': list(_attributes(body, span.log_event)),
        'status': {'code': STATUS_CODE_ERROR if span.is_error else STATUS_CODE_UNSET},
    }
    if span.parent_span_id:
        rendered['parentSpanId'] = _hex_id(span.parent_span_id, width=_SPAN_ID_HEX)
    return rendered


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
