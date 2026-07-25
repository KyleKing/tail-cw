"""Cluster log messages into recurring shapes.

Normalizes variable tokens (timestamps, UUIDs, hex runs, quoted strings, numbers) to
placeholders so identical shapes collapse to one key, then counts the keys. Pure: no AWS
calls, no I/O.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from tail_cw.cache.storage import is_jsonl_message

TS_PLACEHOLDER = '<ts>'
UUID_PLACEHOLDER = '<uuid>'
HEX_PLACEHOLDER = '<hex>'
STR_PLACEHOLDER = '<str>'
NUMBER_PLACEHOLDER = '<n>'

_ISO_TIMESTAMP_RE = re.compile(
    r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?',
)
_LEADING_TIMESTAMP_RE = re.compile(rf'^\s*{_ISO_TIMESTAMP_RE.pattern}\s*')
_EPOCH_RE = re.compile(r'\b(?:\d{13}|\d{10})\b')
_UUID_RE = re.compile(r'\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b', re.IGNORECASE)
_HEX_RE = re.compile(r'\b(?=[0-9a-f]*[a-f])[0-9a-f]{8,}\b', re.IGNORECASE)
_QUOTED_RE = re.compile(r'"[^"\n]*"|\'[^\'\n]*\'')
_FLOAT_RE = re.compile(r'\d+\.\d+(?:[eE][+-]?\d+)?')
_INT_RE = re.compile(r'\d+')
_WHITESPACE_RE = re.compile(r'\s+')
_QUOTED_PLACEHOLDER_RE = re.compile(r'"(<(?:ts|uuid|hex|str|n)>)"')

# Substitution order matters: every later class is a substring of an earlier one, so a UUID
# must be consumed before the hex and digit rules can shred it into fragments.
_SUBSTITUTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (_ISO_TIMESTAMP_RE, TS_PLACEHOLDER),
    (_EPOCH_RE, TS_PLACEHOLDER),
    (_UUID_RE, UUID_PLACEHOLDER),
    (_HEX_RE, HEX_PLACEHOLDER),
    (_QUOTED_RE, STR_PLACEHOLDER),
    (_FLOAT_RE, NUMBER_PLACEHOLDER),
    (_INT_RE, NUMBER_PLACEHOLDER),
)


@dataclass(frozen=True)
class MessagePattern:
    """A recurring message shape with its count and the first message that produced it."""

    key: str
    count: int
    example: str


def normalize_message(message: str) -> str:
    """Reduce a log message to a stable shape key.

    JSON messages normalize their values and keep their keys literal, with keys sorted so
    field order cannot split a shape.
    """
    if is_jsonl_message(message) and (shape := _normalize_jsonl(message)) is not None:
        return shape

    return _normalize_text(message)


def cluster_messages(messages: Iterable[str], *, limit: int = 8) -> list[MessagePattern]:
    """Count message shapes and return the `limit` most common, ties broken by first appearance."""
    counts: Counter[str] = Counter()
    examples: dict[str, str] = {}
    for message in messages:
        key = normalize_message(message)
        counts[key] += 1
        examples.setdefault(key, message)

    appearance = {key: index for index, key in enumerate(examples)}
    ranked = sorted(counts.items(), key=lambda item: (-item[1], appearance[item[0]]))

    return [MessagePattern(key=key, count=count, example=examples[key]) for key, count in ranked[:limit]]


def _normalize_text(message: str) -> str:
    shape = message
    for pattern, placeholder in _SUBSTITUTIONS:
        shape = pattern.sub(placeholder, shape)

    return _WHITESPACE_RE.sub(' ', shape).strip()


def _normalize_jsonl(message: str) -> str | None:
    body = _LEADING_TIMESTAMP_RE.sub('', message, count=1)
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return None

    if not isinstance(parsed, dict):
        return None

    shape = _dump_shape(_normalize_value(parsed))

    return shape if body == message else f'{TS_PLACEHOLDER} {shape}'


def _normalize_value(value: Any) -> Any:
    match value:
        case dict():
            return {key: _normalize_value(item) for key, item in value.items()}
        case list():
            return [_normalize_value(item) for item in value]
        case bool() | None:
            return value
        case int() | float():
            return NUMBER_PLACEHOLDER
        case str():
            return _normalize_text(value)
        case _:
            return value


def _dump_shape(value: Any) -> str:
    dumped = json.dumps(value, sort_keys=True, separators=(',', ':'))

    return _QUOTED_PLACEHOLDER_RE.sub(r'\1', dumped)
