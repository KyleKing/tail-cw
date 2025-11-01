"""Helper functions for formatting and displaying CloudWatch log events.

This module provides pure functions for converting LogEvent instances into
various display formats (table rows, detail views, etc.). Following the
functions-over-classes approach, all logic is implemented as testable,
side-effect-free functions.

Performance considerations:
    - batch_format_log_events() enables efficient bulk processing
    - Rich Text objects are created on-demand for styling
    - Message truncation reduces memory for large datasets

The functions handle edge cases gracefully:
    - None values (e.g., ingestion_time)
    - Empty strings
    - Special characters and unicode
    - Very long field values
    - Malformed JSON in messages
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime

from rich.console import RenderableType
from rich.text import Text

from tail_cw.aws.client import LogEvent
from tail_cw.cache.storage import is_jsonl_message


def format_timestamp(dt: datetime, style: str = 'cyan') -> Text:
    """Format a datetime as a styled Rich Text object.

    Args:
        dt: The datetime to format
        style: Rich style string for the timestamp (default: 'cyan')

    Returns:
        Rich Text object with formatted timestamp and applied style

    Example:
        >>> from datetime import datetime, UTC
        >>> dt = datetime(2025, 1, 15, 10, 30, 45, 123000, tzinfo=UTC)
        >>> formatted = format_timestamp(dt)
        >>> print(formatted.plain)
        '2025-01-15 10:30:45.123'
    """
    # Format with millisecond precision (cut off microseconds to 3 digits)
    formatted_str = dt.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
    return Text(formatted_str, style=style)


def format_log_event_for_table(
    event: LogEvent,
    truncate_message: int = 100,
) -> tuple[RenderableType, str, str, str, str]:
    """Convert a LogEvent to a table row tuple.

    Args:
        event: The log event to format
        truncate_message: Maximum message length for table display (default: 100)

    Returns:
        Tuple of (formatted_timestamp, log_group, log_stream, truncated_message, event_id)

    Example:
        >>> event = LogEvent(...)
        >>> row = format_log_event_for_table(event, truncate_message=50)
        >>> timestamp, group, stream, msg, event_id = row
    """
    # Format timestamp as Rich Text
    formatted_timestamp = format_timestamp(event.timestamp)

    # Truncate message if needed
    if len(event.message) > truncate_message:
        truncated_message = event.message[:truncate_message] + '...'
    else:
        truncated_message = event.message

    return (
        formatted_timestamp,
        event.log_group,
        event.log_stream,
        truncated_message,
        event.event_id,
    )


def batch_format_log_events(
    events: Iterable[LogEvent],
    truncate_message: int = 100,
) -> list[tuple[RenderableType, str, str, str, str]]:
    """Convert multiple LogEvents to table rows efficiently.

    Uses list comprehension for batch processing, improving performance
    when loading large datasets into the UI.

    Args:
        events: Iterator of log events
        truncate_message: Maximum message length for table display (default: 100)

    Returns:
        List of formatted row tuples

    Example:
        >>> events = [event1, event2, event3]
        >>> rows = batch_format_log_events(events)
        >>> table.add_rows(rows)  # Efficient batch insertion
    """
    return [format_log_event_for_table(e, truncate_message) for e in events]


def format_log_event_detail(event: LogEvent) -> str:
    """Format a LogEvent for detailed display.

    Creates a multi-line formatted string with all event fields,
    including full (non-truncated) message.

    Args:
        event: The log event to format

    Returns:
        Multi-line formatted string for detail view

    Example:
        >>> event = LogEvent(...)
        >>> detail = format_log_event_detail(event)
        >>> print(detail)
        Event ID: abc-123
        Timestamp: 2025-01-15T10:30:45.123000+00:00
        ...
    """
    # Format ingestion time, handling None
    ingestion_time_str = event.ingestion_time.isoformat() if event.ingestion_time else 'N/A'

    return f"""Event ID: {event.event_id}
Timestamp: {event.timestamp.isoformat()}
Log Group: {event.log_group}
Log Stream: {event.log_stream}
Ingestion Time: {ingestion_time_str}

Message:
{event.message}"""


def parse_jsonl_message(message: str) -> str | None:
    """Attempt to parse and pretty-print a JSONL message.

    Uses the is_jsonl_message() detector from storage module to check
    if the message is JSON, then attempts to parse and format it.

    Args:
        message: The log message to parse

    Returns:
        Pretty-printed JSON string if message is valid JSON, None otherwise

    Example:
        >>> msg = '{"level":"INFO","message":"test"}'
        >>> parsed = parse_jsonl_message(msg)
        >>> print(parsed)
        {
          "level": "INFO",
          "message": "test"
        }
    """
    # Check if message looks like JSON
    if not is_jsonl_message(message):
        return None

    # Try to parse and format
    try:
        parsed = json.loads(message)
        return json.dumps(parsed, indent=2, sort_keys=True)
    except (json.JSONDecodeError, ValueError):
        # Parsing failed, not valid JSON
        return None


def format_log_event_detail_with_json(event: LogEvent) -> str:
    """Enhanced detail formatter with JSON parsing.

    Attempts to parse the message as JSON. If successful, displays both
    the raw message and the pretty-printed JSON. If not JSON, displays
    only the raw message.

    Args:
        event: The log event to format

    Returns:
        Formatted string with parsed JSON if applicable

    Example:
        >>> event = LogEvent(message='{"key":"value"}', ...)
        >>> detail = format_log_event_detail_with_json(event)
        >>> # Output includes both raw and parsed JSON sections
    """
    # Start with basic detail
    basic_detail = format_log_event_detail(event)

    # Try to parse message as JSON
    parsed_json = parse_jsonl_message(event.message)

    if parsed_json:
        # Replace the Message section with both raw and parsed
        parts = basic_detail.split('Message:\n', 1)
        expected_parts = 2  # header and message content
        if len(parts) == expected_parts:
            header = parts[0]
            return f"""{header}Message (raw):
{event.message}

Message (parsed JSON):
{parsed_json}"""

    # Not JSON or parsing failed, return basic detail
    return basic_detail


def get_column_definitions() -> list[tuple[str, str]]:
    """Return standard column definitions for log tables.

    Centralizes column configuration for consistency across the UI.

    Returns:
        List of (key, label) tuples defining table columns

    Example:
        >>> columns = get_column_definitions()
        >>> for key, label in columns:
        ...     table.add_column(key, label=label)
    """
    return [
        ('timestamp', 'Timestamp'),
        ('log_group', 'Log Group'),
        ('log_stream', 'Log Stream'),
        ('message', 'Message'),
        ('event_id', 'Event ID'),
    ]
