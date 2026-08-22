"""Unit tests for log viewer formatting functions."""

from datetime import UTC, datetime

from rich.text import Text

from tail_cw.tui.log_viewer import (
    batch_format_log_events,
    format_log_event_detail,
    format_log_event_detail_with_json,
    format_log_event_for_table,
    format_timestamp,
    get_column_definitions,
    parse_jsonl_message,
)
from tests.factories import make_event


def test_format_timestamp():
    """Test timestamp formatting with default style."""
    dt = datetime(2025, 1, 15, 10, 30, 45, 123000, tzinfo=UTC)
    result = format_timestamp(dt)

    # Should be a Rich Text object
    assert isinstance(result, Text)

    # Check the string representation
    assert str(result.plain) == '2025-01-15 10:30:45.123'

    # Check default style is cyan
    assert 'cyan' in str(result.style).lower()


def test_format_timestamp_custom_style():
    """Test timestamp formatting with custom style."""
    dt = datetime(2025, 1, 15, 10, 30, 45, 123000, tzinfo=UTC)
    result = format_timestamp(dt, style='red')

    # Should have red style
    assert 'red' in str(result.style).lower()


def test_format_log_event_for_table():
    """Test single event formatting for table."""
    event = make_event(
        timestamp=datetime(2025, 1, 15, 10, 30, 0, tzinfo=UTC),
        message='Short message',
        log_group='/aws/lambda/my-function',
        log_stream='2025/01/15/[$LATEST]abc',
    )

    result = format_log_event_for_table(event)

    # Should be a tuple with 5 elements
    assert isinstance(result, tuple)
    assert len(result) == 4

    # First element should be Rich Text (timestamp)
    assert isinstance(result[0], Text)

    # Other elements should match event fields
    assert result[1] == '/aws/lambda/my-function'
    assert result[2] == '2025/01/15/[$LATEST]abc'
    assert result[3] == 'Short message'  # Not truncated


def test_format_log_event_for_table_truncation():
    """Test message truncation."""
    long_message = 'A' * 150
    event = make_event(message=long_message)

    result = format_log_event_for_table(event, truncate_message=50)

    # Message should be truncated to 50 chars + '...'
    truncated_message = result[3]
    assert len(truncated_message) == 53  # 50 + '...'
    assert truncated_message.endswith('...')
    assert truncated_message.startswith('A')


def test_format_log_event_for_table_no_truncation_needed():
    """Test short messages are not truncated."""
    event = make_event(message='Short')

    result = format_log_event_for_table(event, truncate_message=100)

    # Message should not be modified
    assert result[3] == 'Short'
    assert '...' not in result[3]


def test_batch_format_log_events():
    """Test batch formatting."""
    events = [make_event(f'Message {index}') for index in range(5)]

    result = batch_format_log_events(events)

    assert [row[3] for row in result] == [f'Message {index}' for index in range(5)]
    assert all(len(row) == 4 for row in result)


def test_batch_format_log_events_empty():
    """Test empty input."""
    result = batch_format_log_events([])

    assert result == []


def test_format_log_event_detail():
    """Test detail formatting."""
    event = make_event(
        timestamp=datetime(2025, 1, 15, 10, 30, 45, 123000, tzinfo=UTC),
        message='Test log message',
        log_group='/aws/test/group',
        log_stream='stream-0',
    )

    result = format_log_event_detail(event)

    # Should be a string
    assert isinstance(result, str)

    # Should contain all field labels
    assert 'Timestamp:' in result
    assert 'Log Group:' in result
    assert 'Log Stream:' in result
    assert 'Ingestion Time:' in result
    assert 'Message:' in result

    # Should contain actual values
    assert '2025-01-15' in result
    assert '/aws/test/group' in result
    assert 'stream-0' in result
    assert 'Test log message' in result


def test_format_log_event_detail_no_ingestion_time():
    """Test with None ingestion_time."""
    event = make_event(ingestion_offset=None)

    result = format_log_event_detail(event)

    # Should show 'N/A' for ingestion time
    assert 'Ingestion Time: N/A' in result


def test_parse_jsonl_message_valid_json():
    """Test JSON parsing with valid JSON."""
    message = '{"level":"INFO","msg":"test","index":42}'

    result = parse_jsonl_message(message)

    # Should return formatted JSON
    assert result is not None
    assert isinstance(result, str)

    # Should be indented (pretty-printed)
    assert '\n' in result
    assert '"level"' in result
    assert '"INFO"' in result
    assert '"msg"' in result
    assert '"test"' in result


def test_parse_jsonl_message_invalid_json():
    """Test with malformed JSON."""
    message = '{"level":"INFO", invalid}'

    result = parse_jsonl_message(message)

    # Should return None (parse error)
    assert result is None


def test_parse_jsonl_message_plain_text():
    """Test with plain text message."""
    message = 'This is a plain log message'

    result = parse_jsonl_message(message)

    # Should return None (not JSON)
    assert result is None


def test_format_log_event_detail_with_json():
    """Test enhanced detail with JSON message."""
    json_message = '{"level":"INFO","message":"test event","timestamp":"2025-01-15T10:00:00Z"}'
    event = make_event(message=json_message)

    result = format_log_event_detail_with_json(event)

    # Should contain both raw and parsed sections
    assert 'Message (raw):' in result
    assert 'Message (parsed JSON):' in result

    # Should contain the JSON content
    assert json_message in result
    assert '"level"' in result
    assert '"INFO"' in result


def test_format_log_event_detail_with_json_plain_text():
    """Test with plain text message."""
    event = make_event(message='Plain text log message')

    result = format_log_event_detail_with_json(event)

    # Should contain only the raw message
    assert 'Message:' in result
    assert 'Plain text log message' in result

    # Should NOT contain parsed JSON section
    assert 'Message (parsed JSON):' not in result


def test_get_column_definitions():
    """Test column definitions."""
    result = get_column_definitions()

    # Should be a list of tuples
    assert isinstance(result, list)
    assert len(result) == 4

    # Check structure
    for key, label in result:
        assert isinstance(key, str)
        assert isinstance(label, str)

    # Check specific columns
    assert ('timestamp', 'Timestamp') in result
    assert ('log_group', 'Log Group') in result
    assert ('log_stream', 'Log Stream') in result
    assert ('message', 'Message') in result


def test_special_characters_in_message():
    """Test messages with special characters."""
    special_message = 'Special: <>&"\\n\\t\u2603'
    event = make_event(message=special_message)

    # Test table formatting
    table_row = format_log_event_for_table(event)
    assert special_message in table_row[3]

    # Test detail formatting
    detail = format_log_event_detail(event)
    assert special_message in detail

    # Should not raise exceptions
    assert detail is not None


def test_empty_message():
    """Test empty message field."""
    event = make_event(message='')

    # Test table formatting
    table_row = format_log_event_for_table(event)
    assert not table_row[3]

    # Test detail formatting
    detail = format_log_event_detail(event)
    assert 'Message:\n' in detail

    # Should not raise exceptions
    assert detail is not None


def test_very_long_field_values():
    """Test with extremely long field values."""
    long_group = 'A' * 500
    long_stream = 'B' * 500
    long_message = 'C' * 1000

    event = make_event(
        log_group=long_group,
        log_stream=long_stream,
        message=long_message,
    )

    # Should handle without errors
    table_row = format_log_event_for_table(event)
    detail = format_log_event_detail(event)

    # Table message should be truncated
    assert len(table_row[3]) <= 103  # 100 + '...'

    # Detail should contain full message
    assert long_message in detail


def test_parse_jsonl_message_with_nested_json():
    """Test parsing nested JSON structures."""
    nested_json = '{"outer":{"inner":"value"},"array":[1,2,3]}'

    result = parse_jsonl_message(nested_json)

    assert result is not None
    assert '"outer"' in result
    assert '"inner"' in result
    assert '"array"' in result
    # Should be pretty-printed with indentation
    assert result.count('\n') >= 3


def test_format_timestamp_with_microseconds():
    """Test timestamp formatting preserves milliseconds."""
    # Test various microsecond values
    dt1 = datetime(2025, 1, 15, 10, 30, 45, 0, tzinfo=UTC)
    dt2 = datetime(2025, 1, 15, 10, 30, 45, 123456, tzinfo=UTC)
    dt3 = datetime(2025, 1, 15, 10, 30, 45, 999000, tzinfo=UTC)

    result1 = format_timestamp(dt1)
    result2 = format_timestamp(dt2)
    result3 = format_timestamp(dt3)

    # Should show milliseconds (3 digits)
    assert result1.plain == '2025-01-15 10:30:45.000'
    assert result2.plain == '2025-01-15 10:30:45.123'
    assert result3.plain == '2025-01-15 10:30:45.999'


def test_batch_format_with_different_truncate_lengths():
    """Test batch formatting with custom truncate length."""
    events = [
        make_event(message='A' * 200),
        make_event(message='B' * 200),
    ]

    # Test with different truncation lengths
    result_50 = batch_format_log_events(events, truncate_message=50)
    result_150 = batch_format_log_events(events, truncate_message=150)

    # 50-char truncation
    assert len(result_50[0][3]) == 53  # 50 + '...'
    assert result_50[0][3].startswith('A')

    # 150-char truncation
    assert len(result_150[0][3]) == 153  # 150 + '...'
    assert result_150[0][3].startswith('A')


def test_format_detail_with_multiline_message():
    """Test detail formatting with multi-line messages."""
    multiline_message = """Line 1
Line 2
Line 3"""
    event = make_event(message=multiline_message)

    result = format_log_event_detail(event)

    # All lines should be preserved
    assert 'Line 1' in result
    assert 'Line 2' in result
    assert 'Line 3' in result
    assert result.count('\n') >= 3
