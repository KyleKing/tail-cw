"""Classify a log event's severity from structured fields, falling back to keywords.

Structured fields are authoritative: an event carrying a recognized level or status field
is classified from it alone, so an identifier like ``trace-error`` in a free-text body
cannot promote an informational event. Only events with no structured data at all reach the
keyword scan.
"""

from __future__ import annotations

import contextlib
import json
import re
from collections.abc import Iterator, Mapping
from enum import IntEnum
from typing import Any

from tail_cw.aws.client import LogEvent

ERROR_KEYWORDS = {'error', 'fatal', 'critical', 'exception'}
WARNING_KEYWORDS = {'warn', 'warning'}
ERROR_LEVEL_FIELDS = {'level', 'severity', 'loglevel'}
STATUS_FIELDS = {'status', 'status_code', 'statuscode'}
MESSAGE_FIELDS = {'message', 'msg', 'error_message'}
ERROR_STATUS_THRESHOLD = 500
WARNING_STATUS_THRESHOLD = 400
_ERROR_LEVELS = {'ERROR', 'FATAL', 'CRITICAL'}
_WARNING_LEVELS = {'WARN', 'WARNING'}
# A line that labels its own level says more than a keyword anywhere in its body, so
# "WARNING: Bedrock transient error" is a warning rather than an error. The label may sit
# behind a leading timestamp, and the single-letter form with "!" is what the CloudWatch
# agent and other Go tools emit.
_TIMESTAMP_PREFIX = r'(?:\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?\s+)?'
_LEVEL_WORDS = 'TRACE|DEBUG|INFO|NOTICE|WARN|WARNING|ERROR|FATAL|CRITICAL'
_LEVEL_PREFIX_RE = re.compile(
    rf'^\s*{_TIMESTAMP_PREFIX}(?:[\[\(<]?(?P<word>{_LEVEL_WORDS})[\]\)>]?\s*[:\-|]|(?P<letter>[EWID])!)',
    re.IGNORECASE,
)
_LETTER_LEVELS = {'E': 'ERROR', 'W': 'WARNING', 'I': 'INFO', 'D': 'DEBUG'}


class Severity(IntEnum):
    """Ordered severity, so the highest classification across fields wins."""

    INFO = 0
    WARNING = 1
    ERROR = 2


def load_json_dict(payload: str | None) -> dict[str, Any] | None:
    """Parse a JSON object from payload, or None when it is absent or not an object."""
    if not payload:
        return None

    with contextlib.suppress(json.JSONDecodeError, TypeError):
        parsed = json.loads(payload)
        if isinstance(parsed, dict):
            return parsed
    return None


def iter_structured_event_data(event: LogEvent) -> Iterator[dict[str, Any]]:
    """Yield structured representations of a log event."""
    message_data = load_json_dict(event.message)
    if message_data:
        yield message_data

    parsed_attr = getattr(event, 'parsed', None)
    if isinstance(parsed_attr, dict):
        yield parsed_attr


def event_severity(event: LogEvent) -> Severity:
    """Classify an event as ERROR, WARNING, or INFO."""
    structured_found = False
    highest = Severity.INFO
    for data in iter_structured_event_data(event):
        structured_found = True
        highest = max(highest, _structured_severity(data))
        if highest is Severity.ERROR:
            return highest

    if structured_found:
        return highest

    return keyword_severity(event.message)


def keyword_severity(message: str) -> Severity:
    """Classify free text by its own level prefix when it has one, else by keyword."""
    if match := _LEVEL_PREFIX_RE.match(message):
        word = match.group('word') or _LETTER_LEVELS[match.group('letter').upper()]
        return _level_severity(word.upper())
    lowered = message.lower()
    if any(keyword in lowered for keyword in ERROR_KEYWORDS):
        return Severity.ERROR
    if any(keyword in lowered for keyword in WARNING_KEYWORDS):
        return Severity.WARNING
    return Severity.INFO


def _structured_severity(data: Mapping[str, Any]) -> Severity:
    """Classify a record from its level and status, scanning its text only as a last resort.

    A record that declares its own level is taken at its word: a service logging
    ``{"level": "info", "message": "error finding route"}`` is reporting a routine miss, and
    keyword-scanning the body over the top of that manufactures errors. A status field still
    escalates, because it is structured rather than prose.
    """
    populated = {key.lower(): value for key, value in data.items() if value}
    status = max(
        (_status_severity(value) for key, value in populated.items() if key in STATUS_FIELDS),
        default=Severity.INFO,
    )
    levels = [_level_severity(str(value).upper()) for key, value in populated.items() if key in ERROR_LEVEL_FIELDS]
    if levels:
        return max(*levels, status)
    bodies = [
        keyword_severity(value) for key, value in populated.items() if key in MESSAGE_FIELDS and isinstance(value, str)
    ]
    return max([status, *bodies])


def _level_severity(level: str) -> Severity:
    if level in _ERROR_LEVELS:
        return Severity.ERROR
    if level in _WARNING_LEVELS:
        return Severity.WARNING
    return Severity.INFO


def _status_severity(value: Any) -> Severity:
    with contextlib.suppress(ValueError, TypeError):
        status = int(value)
        if status >= ERROR_STATUS_THRESHOLD:
            return Severity.ERROR
        if status >= WARNING_STATUS_THRESHOLD:
            return Severity.WARNING
    return Severity.INFO
