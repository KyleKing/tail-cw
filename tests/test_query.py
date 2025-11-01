"""Tests for query module (parser and engine)."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tail_cw.aws.client import LogEvent
from tail_cw.cache.storage import write_log_events_to_parquet
from tail_cw.query.engine import (
    QueryBackend,
    _select_backend,
    benchmark_backends,
    query_parquet_file,
    query_parquet_file_to_log_events,
)
from tail_cw.query.parser import (
    FilterNode,
    FilterNodeType,
    combine_filters,
    filter_to_string,
    parse_extended_filter,
    parse_filter_pattern,
)


def _make_log_event(
    *,
    log_group: str = '/aws/lambda/test',
    log_stream: str = '2025/01/01/stream',
    timestamp: datetime | None = None,
    message: str = 'Test message',
    event_id: str | None = None,
    ingestion_time: datetime | None = None,
) -> LogEvent:
    """Create a test LogEvent instance.

    Args:
        log_group: Log group name
        log_stream: Log stream name
        timestamp: Event timestamp (defaults to current UTC time)
        message: Log message
        event_id: Event ID (auto-generated if None)
        ingestion_time: Optional ingestion time

    Returns:
        LogEvent instance for testing
    """
    if timestamp is None:
        timestamp = datetime.now(tz=UTC)

    if event_id is None:
        event_id = f'event-{hash(message) % 100000}'

    return LogEvent(
        log_group=log_group,
        log_stream=log_stream,
        timestamp=timestamp,
        message=message,
        event_id=event_id,
        ingestion_time=ingestion_time,
    )


def _make_test_parquet_file(
    output_path: Path,
    *,
    include_jsonl: bool = True,
    count: int = 10,
) -> list[LogEvent]:
    """Create a test Parquet file with sample data.

    Args:
        output_path: Where to write Parquet file
        include_jsonl: Whether to include JSONL messages
        count: Number of events to create

    Returns:
        List of events written to file
    """
    events = []
    base_time = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)

    for i in range(count):
        # Mix plain text and JSONL messages
        if include_jsonl and i % 2 == 0:
            # JSONL message with various fields
            if i % 4 == 0:
                message = f'{{"level":"ERROR","msg":"Error {i}","status":500}}'
            else:
                message = f'{{"level":"INFO","msg":"Info {i}","status":200}}'
        else:
            # Plain text message
            message = f'Plain log message {i} with ERROR' if i % 3 == 0 else f'Plain log message {i}'

        event = _make_log_event(
            message=message,
            timestamp=base_time + timedelta(seconds=i),
            event_id=f'event-{i}',
        )
        events.append(event)

    # Write to Parquet
    write_log_events_to_parquet(events, output_path)
    return events


# ============================================================================
# Parser Tests
# ============================================================================


def test_parse_filter_pattern_empty():
    """Test empty pattern returns match-all node."""
    node = parse_filter_pattern('')
    assert node.node_type == FilterNodeType.MATCH_ALL


def test_parse_filter_pattern_text_search():
    """Test plain text search parsing."""
    node = parse_filter_pattern('ERROR')
    assert node.node_type == FilterNodeType.TEXT_SEARCH
    assert node.value == 'ERROR'


def test_parse_filter_pattern_exact_phrase():
    """Test quoted phrase parsing."""
    node = parse_filter_pattern('"connection timeout"')
    assert node.node_type == FilterNodeType.EXACT_PHRASE
    assert node.value == 'connection timeout'


def test_parse_filter_pattern_regex():
    """Test regex pattern parsing."""
    node = parse_filter_pattern('%[Ee]rror%')
    assert node.node_type == FilterNodeType.REGEX
    assert node.value == '[Ee]rror'


def test_parse_filter_pattern_json_field_equals():
    """Test JSON field equality parsing."""
    node = parse_filter_pattern('{ $.level = "ERROR" }')
    assert node.node_type == FilterNodeType.JSON_FIELD_EQUALS
    assert node.field_path == ['level']
    assert node.value == 'ERROR'


def test_parse_filter_pattern_json_field_numeric():
    """Test numeric comparison parsing."""
    node = parse_filter_pattern('{ $.status >= 500 }')
    assert node.node_type == FilterNodeType.JSON_FIELD_NUMERIC
    assert node.field_path == ['status']
    assert node.operator == '>='
    assert node.value == '500'


def test_parse_filter_pattern_json_field_exists():
    """Test field existence check parsing."""
    node = parse_filter_pattern('{ $.userId = * }')
    assert node.node_type == FilterNodeType.JSON_FIELD_EXISTS
    assert node.field_path == ['userId']


def test_parse_filter_pattern_json_multiple_conditions():
    """Test multiple JSON conditions (implicit AND)."""
    node = parse_filter_pattern('{ $.level = "ERROR" $.status >= 500 }')
    assert node.node_type == FilterNodeType.AND
    assert node.children is not None
    assert len(node.children) == 2

    # Verify first condition
    child1 = node.children[0]
    assert child1.node_type == FilterNodeType.JSON_FIELD_EQUALS
    assert child1.field_path == ['level']
    assert child1.value == 'ERROR'

    # Verify second condition
    child2 = node.children[1]
    assert child2.node_type == FilterNodeType.JSON_FIELD_NUMERIC
    assert child2.field_path == ['status']


def test_parse_filter_pattern_nested_field_path():
    """Test nested JSON field path parsing."""
    node = parse_filter_pattern('{ $.context.user.id = "123" }')
    assert node.field_path == ['context', 'user', 'id']
    assert node.value == '123'


def test_parse_extended_filter_simple():
    """Test extended key:value syntax."""
    node = parse_extended_filter('level:ERROR')
    assert node.node_type == FilterNodeType.JSON_FIELD_EQUALS
    assert node.field_path == ['level']
    assert node.value == 'ERROR'


def test_parse_extended_filter_numeric():
    """Test extended numeric comparison."""
    node = parse_extended_filter('status:>=500')
    assert node.node_type == FilterNodeType.JSON_FIELD_NUMERIC
    assert node.field_path == ['status']
    assert node.operator == '>='
    assert node.value == '500'


def test_parse_extended_filter_nested():
    """Test extended nested field syntax."""
    node = parse_extended_filter('user.id:123')
    assert node.field_path == ['user', 'id']
    assert node.value == '123'


def test_parse_extended_filter_exists():
    """Test extended field existence check."""
    node = parse_extended_filter('userId:*')
    assert node.node_type == FilterNodeType.JSON_FIELD_EXISTS
    assert node.field_path == ['userId']


def test_combine_filters_and():
    """Test combining filters with AND."""
    node1 = FilterNode(node_type=FilterNodeType.TEXT_SEARCH, value='ERROR')
    node2 = FilterNode(node_type=FilterNodeType.TEXT_SEARCH, value='WARN')

    combined = combine_filters([node1, node2], 'AND')

    assert combined.node_type == FilterNodeType.AND
    assert combined.children == [node1, node2]


def test_combine_filters_or():
    """Test combining filters with OR."""
    node1 = FilterNode(node_type=FilterNodeType.TEXT_SEARCH, value='ERROR')
    node2 = FilterNode(node_type=FilterNodeType.TEXT_SEARCH, value='WARN')

    combined = combine_filters([node1, node2], 'OR')

    assert combined.node_type == FilterNodeType.OR
    assert combined.children == [node1, node2]


def test_combine_filters_single():
    """Test combining single filter returns node directly."""
    node = FilterNode(node_type=FilterNodeType.TEXT_SEARCH, value='ERROR')
    combined = combine_filters([node], 'AND')
    assert combined is node


def test_filter_to_string():
    """Test converting filter to string representation."""
    node = FilterNode(node_type=FilterNodeType.TEXT_SEARCH, value='ERROR')
    result = filter_to_string(node)
    assert 'TEXT_SEARCH' in result
    assert 'ERROR' in result


def test_parse_filter_pattern_invalid():
    """Test that invalid patterns raise ValueError."""
    # Invalid JSON filter (no closing brace)
    with pytest.raises(ValueError, match='Mismatched braces'):
        parse_filter_pattern('{ $.level = "ERROR"')

    # Invalid regex (unclosed bracket)
    with pytest.raises(ValueError, match='Invalid regex pattern'):
        parse_filter_pattern('%[Ee%')


def test_parse_filter_pattern_multiple_text_terms():
    """Test multiple text terms create AND node."""
    node = parse_filter_pattern('ERROR timeout')
    assert node.node_type == FilterNodeType.AND
    assert node.children is not None
    assert len(node.children) == 2


# ============================================================================
# Engine Tests - DuckDB Backend
# ============================================================================


def test_query_parquet_file_duckdb_no_filter(fix_test_cache):
    """Test querying without filter using DuckDB."""
    parquet_path = fix_test_cache / 'test_no_filter.parquet'
    events = _make_test_parquet_file(parquet_path, count=10)

    results = list(
        query_parquet_file(
            parquet_path,
            None,
            backend=QueryBackend.DUCKDB,
        ),
    )

    assert len(results) == len(events)


def test_query_parquet_file_duckdb_text_search(fix_test_cache):
    """Test text search with DuckDB."""
    parquet_path = fix_test_cache / 'test_text_search.parquet'
    _make_test_parquet_file(parquet_path, count=10)

    filter_node = parse_filter_pattern('ERROR')
    results = list(
        query_parquet_file(
            parquet_path,
            filter_node,
            backend=QueryBackend.DUCKDB,
        ),
    )

    # Should only return events containing 'ERROR'
    assert len(results) > 0
    for row in results:
        assert 'ERROR' in row['message'].upper()


def test_query_parquet_file_duckdb_json_field(fix_test_cache):
    """Test JSON field filter with DuckDB."""
    parquet_path = fix_test_cache / 'test_json_field.parquet'
    _make_test_parquet_file(parquet_path, include_jsonl=True, count=10)

    filter_node = parse_filter_pattern('{ $.level = "ERROR" }')
    results = list(
        query_parquet_file(
            parquet_path,
            filter_node,
            backend=QueryBackend.DUCKDB,
        ),
    )

    # Should only return JSONL events with level=ERROR
    assert len(results) > 0
    for row in results:
        # Verify message contains level:ERROR
        assert 'level":"ERROR"' in row['message'] or '"level":"ERROR"' in row['message']


def test_query_parquet_file_duckdb_numeric_comparison(fix_test_cache):
    """Test numeric comparison with DuckDB."""
    parquet_path = fix_test_cache / 'test_numeric.parquet'
    _make_test_parquet_file(parquet_path, include_jsonl=True, count=10)

    filter_node = parse_filter_pattern('{ $.status >= 500 }')
    results = list(
        query_parquet_file(
            parquet_path,
            filter_node,
            backend=QueryBackend.DUCKDB,
        ),
    )

    # Should return events with status >= 500
    assert len(results) > 0


def test_query_parquet_file_duckdb_limit(fix_test_cache):
    """Test result limit with DuckDB."""
    parquet_path = fix_test_cache / 'test_limit.parquet'
    _make_test_parquet_file(parquet_path, count=100)

    results = list(
        query_parquet_file(
            parquet_path,
            None,
            backend=QueryBackend.DUCKDB,
            limit=10,
        ),
    )

    assert len(results) == 10


# ============================================================================
# Engine Tests - Polars Backend
# ============================================================================


def test_query_parquet_file_polars_no_filter(fix_test_cache):
    """Test querying without filter using Polars."""
    parquet_path = fix_test_cache / 'test_polars_no_filter.parquet'
    events = _make_test_parquet_file(parquet_path, count=10)

    results = list(
        query_parquet_file(
            parquet_path,
            None,
            backend=QueryBackend.POLARS,
        ),
    )

    assert len(results) == len(events)


def test_query_parquet_file_polars_text_search(fix_test_cache):
    """Test text search with Polars."""
    parquet_path = fix_test_cache / 'test_polars_text.parquet'
    _make_test_parquet_file(parquet_path, count=10)

    filter_node = parse_filter_pattern('ERROR')
    results = list(
        query_parquet_file(
            parquet_path,
            filter_node,
            backend=QueryBackend.POLARS,
        ),
    )

    assert len(results) > 0
    for row in results:
        assert 'ERROR' in row['message'].upper()


def test_query_parquet_file_polars_json_field(fix_test_cache):
    """Test JSON field filter with Polars."""
    parquet_path = fix_test_cache / 'test_polars_json.parquet'
    _make_test_parquet_file(parquet_path, include_jsonl=True, count=10)

    filter_node = parse_filter_pattern('{ $.level = "ERROR" }')
    results = list(
        query_parquet_file(
            parquet_path,
            filter_node,
            backend=QueryBackend.POLARS,
        ),
    )

    assert len(results) > 0


def test_query_parquet_file_polars_numeric_comparison(fix_test_cache):
    """Test numeric comparison with Polars."""
    parquet_path = fix_test_cache / 'test_polars_numeric.parquet'
    _make_test_parquet_file(parquet_path, include_jsonl=True, count=10)

    filter_node = parse_filter_pattern('{ $.status >= 500 }')
    results = list(
        query_parquet_file(
            parquet_path,
            filter_node,
            backend=QueryBackend.POLARS,
        ),
    )

    assert len(results) > 0


def test_query_parquet_file_polars_limit(fix_test_cache):
    """Test result limit with Polars."""
    parquet_path = fix_test_cache / 'test_polars_limit.parquet'
    _make_test_parquet_file(parquet_path, count=100)

    results = list(
        query_parquet_file(
            parquet_path,
            None,
            backend=QueryBackend.POLARS,
            limit=10,
        ),
    )

    assert len(results) == 10


# ============================================================================
# Backend Selection Tests
# ============================================================================


def test_select_backend_no_filter():
    """Test auto-selection with no filter returns Polars."""
    backend = _select_backend(None)
    assert backend == QueryBackend.POLARS


def test_select_backend_simple_text():
    """Test auto-selection with simple text search returns Polars."""
    node = FilterNode(node_type=FilterNodeType.TEXT_SEARCH, value='ERROR')
    backend = _select_backend(node)
    assert backend == QueryBackend.POLARS


def test_select_backend_regex():
    """Test auto-selection with regex returns DuckDB."""
    node = FilterNode(node_type=FilterNodeType.REGEX, value='[Ee]rror')
    backend = _select_backend(node)
    assert backend == QueryBackend.DUCKDB


def test_select_backend_nested_json():
    """Test auto-selection with deep nested field returns DuckDB."""
    node = FilterNode(
        node_type=FilterNodeType.JSON_FIELD_EQUALS,
        field_path=['context', 'user', 'id'],
        value='123',
    )
    backend = _select_backend(node)
    assert backend == QueryBackend.DUCKDB


# ============================================================================
# Integration Tests
# ============================================================================


def test_query_parquet_file_to_log_events(fix_test_cache):
    """Test convenience wrapper returns LogEvent instances."""
    parquet_path = fix_test_cache / 'test_to_log_events.parquet'
    events = _make_test_parquet_file(parquet_path, count=5)

    results = list(query_parquet_file_to_log_events(parquet_path, None))

    assert len(results) == len(events)
    for result in results:
        assert isinstance(result, LogEvent)
        assert result.log_group is not None
        assert result.message is not None


def test_query_parquet_file_auto_backend(fix_test_cache):
    """Test AUTO backend selection works correctly."""
    parquet_path = fix_test_cache / 'test_auto_backend.parquet'
    _make_test_parquet_file(parquet_path, count=10)

    filter_node = parse_filter_pattern('ERROR')
    results = list(
        query_parquet_file(
            parquet_path,
            filter_node,
            backend=QueryBackend.AUTO,
        ),
    )

    assert len(results) > 0


def test_benchmark_backends(fix_test_cache):
    """Test performance benchmarking function."""
    parquet_path = fix_test_cache / 'test_benchmark.parquet'
    _make_test_parquet_file(parquet_path, count=50)

    filter_node = parse_filter_pattern('ERROR')
    times = benchmark_backends(parquet_path, filter_node, iterations=2)

    assert 'duckdb' in times
    assert 'polars' in times
    assert times['duckdb'] > 0
    assert times['polars'] > 0


# ============================================================================
# Edge Cases and Error Handling
# ============================================================================


def test_query_parquet_file_nonexistent():
    """Test querying nonexistent file raises error."""
    with pytest.raises(FileNotFoundError):
        list(query_parquet_file(Path('nonexistent.parquet'), None))


def test_query_parquet_file_empty_results(fix_test_cache):
    """Test query with no matches returns empty iterator."""
    parquet_path = fix_test_cache / 'test_empty_results.parquet'
    _make_test_parquet_file(parquet_path, count=10)

    # Search for something that doesn't exist
    filter_node = parse_filter_pattern('NONEXISTENT_PATTERN_XYZABC')
    results = list(query_parquet_file(parquet_path, filter_node))

    assert len(results) == 0


def test_query_parquet_file_special_characters(fix_test_cache):
    """Test querying with special characters in messages."""
    parquet_path = fix_test_cache / 'test_special_chars.parquet'

    # Create events with special characters
    events = [
        _make_log_event(message='Log with "quotes" and \\backslash'),
        _make_log_event(message="Log with 'single quotes' and newline\n"),
        _make_log_event(message='Log with unicode: café ☕'),
    ]
    write_log_events_to_parquet(events, parquet_path)

    # Query for quotes
    filter_node = parse_filter_pattern('quotes')
    results = list(query_parquet_file(parquet_path, filter_node))

    assert len(results) >= 1


# ============================================================================
# Regex and Nested JSON Tests
# ============================================================================


def test_query_parquet_file_duckdb_regex_execution(fix_test_cache):
    """Test executing regex filters with DuckDB backend."""
    parquet_path = fix_test_cache / 'test_regex_execution.parquet'

    # Create events with mixed case error messages
    events = [
        _make_log_event(message='Error occurred in system'),
        _make_log_event(message='WARNING: Connection failed'),
        _make_log_event(message='Info: everything is fine'),
        _make_log_event(message='error: something went wrong'),
    ]
    write_log_events_to_parquet(events, parquet_path)

    # Test regex pattern for [Ee]rror (case-sensitive)
    filter_node = parse_filter_pattern('%[Ee]rror%')
    results = list(
        query_parquet_file(
            parquet_path,
            filter_node,
            backend=QueryBackend.DUCKDB,
        ),
    )

    # Should match messages with "Error" or "error" (but not "WARNING")
    assert len(results) == 2
    for row in results:
        assert 'error' in row['message'].lower()
        assert 'warning' not in row['message'].lower()


def test_query_parquet_file_polars_regex_execution(fix_test_cache):
    """Test executing regex filters with Polars backend."""
    parquet_path = fix_test_cache / 'test_polars_regex_execution.parquet'

    # Create events with mixed case error messages
    events = [
        _make_log_event(message='Error occurred in system'),
        _make_log_event(message='WARNING: Connection failed'),
        _make_log_event(message='Info: everything is fine'),
        _make_log_event(message='error: something went wrong'),
    ]
    write_log_events_to_parquet(events, parquet_path)

    # Test regex pattern for [Ee]rror (case-sensitive)
    filter_node = parse_filter_pattern('%[Ee]rror%')
    results = list(
        query_parquet_file(
            parquet_path,
            filter_node,
            backend=QueryBackend.POLARS,
        ),
    )

    # Should match messages with "Error" or "error" (but not "WARNING")
    assert len(results) == 2
    for row in results:
        assert 'error' in row['message'].lower()
        assert 'warning' not in row['message'].lower()


def _make_test_parquet_file_with_nested_json(
    output_path: Path,
    count: int = 10,
) -> list[LogEvent]:
    """Create a test Parquet file with nested JSON data.

    Args:
        output_path: Where to write Parquet file
        count: Number of events to create

    Returns:
        List of events written to file
    """
    events = []
    base_time = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)

    for i in range(count):
        # Create nested JSON messages
        if i % 3 == 0:
            nested_data = {
                'level': 'ERROR',
                'context': {
                    'user': {
                        'id': f'user-{i}',
                        'name': f'User {i}',
                    },
                    'request_id': f'req-{i}',
                },
                'status': 500,
            }
        elif i % 3 == 1:
            nested_data = {
                'level': 'INFO',
                'context': {
                    'user': {
                        'id': f'user-{i}',
                        'name': f'User {i}',
                    },
                    'request_id': f'req-{i}',
                },
                'status': 200,
            }
        else:
            nested_data = {
                'level': 'WARN',
                'message': f'Warning {i}',
                'status': 404,
            }

        message = json.dumps(nested_data)
        event = _make_log_event(
            message=message,
            timestamp=base_time + timedelta(seconds=i),
            event_id=f'event-{i}',
        )
        events.append(event)

    # Write to Parquet
    write_log_events_to_parquet(events, output_path)
    return events


def test_query_parquet_file_nested_json_duckdb(fix_test_cache):
    """Test nested JSON field filter execution with DuckDB."""
    parquet_path = fix_test_cache / 'test_nested_json_duckdb.parquet'
    _make_test_parquet_file_with_nested_json(parquet_path, count=9)

    # Test nested field access: $.context.user.id = "user-0"
    filter_node = parse_filter_pattern('{ $.context.user.id = "user-0" }')
    results = list(
        query_parquet_file(
            parquet_path,
            filter_node,
            backend=QueryBackend.DUCKDB,
        ),
    )

    # Should find the event with user-0
    assert len(results) == 1
    assert 'user-0' in results[0]['message']


def test_query_parquet_file_nested_json_polars(fix_test_cache):
    """Test nested JSON field filter execution with Polars."""
    parquet_path = fix_test_cache / 'test_nested_json_polars.parquet'
    _make_test_parquet_file_with_nested_json(parquet_path, count=9)

    # Test nested field access: $.context.user.id = "user-3"
    filter_node = parse_filter_pattern('{ $.context.user.id = "user-3" }')
    results = list(
        query_parquet_file(
            parquet_path,
            filter_node,
            backend=QueryBackend.POLARS,
        ),
    )

    # Should find the event with user-3
    assert len(results) == 1
    assert 'user-3' in results[0]['message']


def test_query_parquet_file_nested_json_multiple_levels(fix_test_cache):
    """Test deeply nested JSON field filter execution."""
    parquet_path = fix_test_cache / 'test_nested_json_deep.parquet'
    _make_test_parquet_file_with_nested_json(parquet_path, count=9)

    # Test multiple nested levels with DuckDB
    filter_node = parse_filter_pattern('{ $.context.user.name = "User 6" }')
    results = list(
        query_parquet_file(
            parquet_path,
            filter_node,
            backend=QueryBackend.DUCKDB,
        ),
    )

    # Should find events with "User 6"
    assert len(results) == 1
    assert 'User 6' in results[0]['message']
