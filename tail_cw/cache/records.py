"""How a cached record is read back as text, with no storage engine behind it.

Pattern shaping and the log table want these questions answered without
importing Polars, so they live apart from :mod:`tail_cw.cache.storage`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

_TIMESTAMP_PREFIX_RE = re.compile(
    r'^\s*\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?\s*',
)


def strip_timestamp_prefix(message: str) -> str:
    """Drop a leading ISO timestamp, which the table's own Timestamp column already shows."""
    return _TIMESTAMP_PREFIX_RE.sub('', message, count=1)


def is_jsonl_message(message: str) -> bool:
    """Detect if a log message appears to be JSON.

    Checks if the message starts with '{' after stripping leading whitespace.
    Also handles messages with leading ISO8601/RFC3339 timestamps followed by JSON.

    Args:
        message: The log message content.

    Returns:
        True if message appears to be JSON, False otherwise.

    Example:
        >>> is_jsonl_message('{"level":"INFO","msg":"test"}')
        True
        >>> is_jsonl_message('  {"key":"value"}')
        True
        >>> is_jsonl_message('2025-01-01T12:00:00Z {"k":1}')
        True
        >>> is_jsonl_message('Plain text log')
        False
    """
    # Fast path: check if message starts with '{' after stripping whitespace
    stripped = message.lstrip()
    if stripped.startswith('{'):
        return True

    # Check if message has timestamp prefix followed by '{'
    # Remove timestamp prefix and check again
    without_timestamp = _TIMESTAMP_PREFIX_RE.sub('', message, count=1)
    return without_timestamp.lstrip().startswith('{')


def readable_message(row: Mapping[str, Any]) -> str:
    """Return an event's text, rebuilt from ``parsed`` when the raw line was not stored.

    Args:
        row: One row of a cached Parquet file, keyed by column name.
    """
    message = row.get('message')
    if message is not None:
        return str(message)
    parsed = row.get('parsed')
    return json.dumps(without_nulls(parsed), separators=(',', ':')) if parsed is not None else ''


def without_nulls(value: Any) -> Any:
    """Drop the null fields Polars adds when widening a struct across records.

    A struct column carries every key any record in the file used, so a record
    that never had a key reads back holding it as null. Emitting those would
    describe the file rather than the event.
    """
    if isinstance(value, dict):
        return {key: without_nulls(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [without_nulls(item) for item in value]
    return value
