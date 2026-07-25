"""Query engine for Parquet files with dual backend support.

Provides efficient querying of Parquet files containing CloudWatch log events
with automatic backend selection between DuckDB (for complex queries) and
Polars (for simple queries and full scans).

Supports CloudWatch-style filter patterns translated to backend-specific queries.
"""

from __future__ import annotations

import heapq
import re
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import datetime
from enum import Enum
from itertools import islice
from pathlib import Path
from typing import Any

import duckdb
import polars as pl

from tail_cw.aws.client import LogEvent
from tail_cw.query.parser import FilterNode, FilterNodeType

MAX_POLARS_FIELD_DEPTH = 2


class QueryBackend(Enum):
    """Query backend type."""

    DUCKDB = 'duckdb'  # Use DuckDB for querying
    POLARS = 'polars'  # Use Polars for querying
    AUTO = 'auto'  # Automatically select based on query characteristics


def query_parquet_file(
    parquet_path: Path,
    filter_node: FilterNode | None = None,
    *,
    backend: QueryBackend = QueryBackend.AUTO,
    limit: int | None = None,
) -> Iterator[dict[str, Any]]:
    """Query a Parquet file containing log events.

    Args:
        parquet_path: Path to Parquet file to query
        filter_node: Parsed filter AST (None = return all records)
        backend: Backend to use (DUCKDB, POLARS, or AUTO)
        limit: Maximum number of results to return (None = no limit)

    Yields:
        Dict for each matching log event row

    Raises:
        FileNotFoundError: If the Parquet file is missing

    Examples:
        >>> from tail_cw.query.parser import parse_filter_pattern
        >>> filter_node = parse_filter_pattern('ERROR')
        >>> for row in query_parquet_file(Path('logs.parquet'), filter_node):
        ...     print(row['message'])
    """
    if not parquet_path.exists():
        msg = f'Parquet file not found: {parquet_path}'
        raise FileNotFoundError(msg)

    # Select backend
    selected_backend = backend
    if backend == QueryBackend.AUTO:
        selected_backend = _select_backend(filter_node)

    # Execute query with selected backend
    if selected_backend == QueryBackend.DUCKDB:
        yield from _query_with_duckdb(parquet_path, filter_node, limit)
    else:  # POLARS
        yield from _query_with_polars(parquet_path, filter_node, limit)


def _select_backend(filter_node: FilterNode | None) -> QueryBackend:
    """Automatically select optimal backend based on query characteristics.

    Heuristics:
    - No filter (full scan): Polars (faster for full scans)
    - Complex nested JSON field access (>2 levels): DuckDB (better SQL support)
    - Regex patterns: DuckDB (better regex support)
    - Simple text search or single-level JSON: Polars (faster)
    - Default: Polars

    Args:
        filter_node: Parsed filter AST

    Returns:
        Selected backend (DUCKDB or POLARS)

    Examples:
        >>> _select_backend(None)
        QueryBackend.POLARS
    """
    if filter_node is None:
        # No filter - use Polars for fast full scan
        return QueryBackend.POLARS

    # Check for regex - prefer DuckDB
    if _has_regex(filter_node):
        return QueryBackend.DUCKDB

    # Check for complex nested JSON fields - prefer DuckDB
    max_depth = _max_field_path_depth(filter_node)
    if max_depth > MAX_POLARS_FIELD_DEPTH:
        return QueryBackend.DUCKDB

    # Default to Polars for simple queries
    return QueryBackend.POLARS


def _has_regex(node: FilterNode) -> bool:
    """Check if filter tree contains any regex patterns.

    Args:
        node: Filter node to check

    Returns:
        True if tree contains regex patterns
    """
    if node.node_type == FilterNodeType.REGEX:
        return True

    if node.children:
        return any(_has_regex(child) for child in node.children)

    return False


def _max_field_path_depth(node: FilterNode) -> int:
    """Find maximum field path depth in filter tree.

    Args:
        node: Filter node to check

    Returns:
        Maximum depth (1 = single field, 2 = field.subfield, etc.)
    """
    max_depth = 0

    if node.field_path:
        max_depth = len(node.field_path)

    if node.children:
        child_depths = [_max_field_path_depth(child) for child in node.children]
        max_depth = max(max_depth, *child_depths)

    return max_depth


def _query_with_duckdb(
    parquet_path: Path,
    filter_node: FilterNode | None,
    limit: int | None,
) -> Iterator[dict[str, Any]]:
    """Execute query using DuckDB backend.

    Args:
        parquet_path: Parquet file path
        filter_node: Filter AST
        limit: Result limit

    Yields:
        Dict for each matching row

    Raises:
        ValueError: If query fails
    """
    try:
        with duckdb.connect() as con:
            sql = 'SELECT * FROM read_parquet(?)'
            params: list[Any] = [str(parquet_path)]

            if filter_node is not None and filter_node.node_type != FilterNodeType.MATCH_ALL:
                where_clause = _build_duckdb_where_clause(filter_node)
                sql += f' WHERE {where_clause}'

            if limit is not None:
                sql += ' LIMIT ?'
                params.append(limit)

            cursor = con.execute(sql, params)
            column_names = [description[0] for description in cursor.description or []]

            for row in cursor.fetchall():
                yield dict(zip(column_names, row, strict=False))

    except duckdb.Error as e:
        msg = f'DuckDB query failed: {e}'
        raise ValueError(msg) from e


def _build_duckdb_where_clause(node: FilterNode) -> str:
    """Translate FilterNode to a DuckDB SQL WHERE clause snippet.

    Returns:
        SQL WHERE clause without the leading `WHERE`.

    Raises:
        ValueError: If the node type is unsupported or malformed.
    """
    try:
        builder = _DUCKDB_BUILDERS[node.node_type]
    except KeyError as err:
        msg = f'Unsupported filter node type: {node.node_type}'
        raise ValueError(msg) from err

    return builder(node)


def _duckdb_clause_text_search(node: FilterNode) -> str:
    value_escaped = _escape_sql_string(node.value or '')
    return f"LOWER(message) LIKE LOWER('%{value_escaped}%')"


def _duckdb_clause_exact_phrase(node: FilterNode) -> str:
    value_escaped = _escape_sql_string(node.value or '')
    return f"LOWER(message) LIKE LOWER('%{value_escaped}%')"


def _duckdb_clause_regex(node: FilterNode) -> str:
    pattern_escaped = _escape_sql_string(node.value or '')
    return f"regexp_matches(message, '{pattern_escaped}')"


def _duckdb_clause_json_equals(node: FilterNode) -> str:
    field_ref = _build_duckdb_field_reference(node.field_path or [])
    value_escaped = _escape_sql_string(node.value or '')
    return f"(parsed IS NOT NULL AND {field_ref} = '{value_escaped}')"


def _duckdb_clause_json_not_equals(node: FilterNode) -> str:
    field_ref = _build_duckdb_field_reference(node.field_path or [])
    value_escaped = _escape_sql_string(node.value or '')
    return f"(parsed IS NOT NULL AND {field_ref} != '{value_escaped}')"


def _duckdb_clause_json_exists(node: FilterNode) -> str:
    field_ref = _build_duckdb_field_reference(node.field_path or [])
    return f'(parsed IS NOT NULL AND {field_ref} IS NOT NULL)'


def _duckdb_clause_json_numeric(node: FilterNode) -> str:
    field_ref = _build_duckdb_field_reference(node.field_path or [])
    operator = node.operator or '='
    value = node.value or '0'
    return f'(parsed IS NOT NULL AND CAST({field_ref} AS DOUBLE) {operator} {value})'


def _duckdb_clause_json_regex(node: FilterNode) -> str:
    field_ref = _build_duckdb_field_reference(node.field_path or [])
    pattern_escaped = _escape_sql_string(node.value or '')
    return f"(parsed IS NOT NULL AND regexp_matches({field_ref}, '{pattern_escaped}'))"


def _duckdb_clause_and(node: FilterNode) -> str:
    if not node.children:
        msg = 'AND node must have children'
        raise ValueError(msg)

    child_clauses = [_build_duckdb_where_clause(child) for child in node.children]
    return f'({" AND ".join(child_clauses)})'


def _duckdb_clause_or(node: FilterNode) -> str:
    if not node.children:
        msg = 'OR node must have children'
        raise ValueError(msg)

    child_clauses = [_build_duckdb_where_clause(child) for child in node.children]
    return f'({" OR ".join(child_clauses)})'


def _duckdb_clause_not(node: FilterNode) -> str:
    if not node.children or len(node.children) != 1:
        msg = 'NOT node must have exactly one child'
        raise ValueError(msg)

    child_clause = _build_duckdb_where_clause(node.children[0])
    return f'NOT ({child_clause})'


DuckDBClauseBuilder = Callable[[FilterNode], str]

_DUCKDB_BUILDERS: dict[FilterNodeType, DuckDBClauseBuilder] = {
    FilterNodeType.TEXT_SEARCH: _duckdb_clause_text_search,
    FilterNodeType.EXACT_PHRASE: _duckdb_clause_exact_phrase,
    FilterNodeType.REGEX: _duckdb_clause_regex,
    FilterNodeType.JSON_FIELD_EQUALS: _duckdb_clause_json_equals,
    FilterNodeType.JSON_FIELD_NOT_EQUALS: _duckdb_clause_json_not_equals,
    FilterNodeType.JSON_FIELD_EXISTS: _duckdb_clause_json_exists,
    FilterNodeType.JSON_FIELD_NUMERIC: _duckdb_clause_json_numeric,
    FilterNodeType.JSON_FIELD_REGEX: _duckdb_clause_json_regex,
    FilterNodeType.AND: _duckdb_clause_and,
    FilterNodeType.OR: _duckdb_clause_or,
    FilterNodeType.NOT: _duckdb_clause_not,
}


def _build_duckdb_field_reference(field_path: list[str]) -> str:
    """Build DuckDB struct field reference from field path.

    Args:
        field_path: List of field components (e.g., ['level'] or ['context', 'user', 'id'])

    Returns:
        DuckDB field reference (e.g., "parsed.level" or "parsed['key-with-hyphen']")

    Examples:
        >>> _build_duckdb_field_reference(['level'])
        'parsed.level'

        >>> _build_duckdb_field_reference(['context', 'user', 'id'])
        'parsed.context.user.id'

        >>> _build_duckdb_field_reference(['key-with-hyphen'])
        "parsed['key-with-hyphen']"
    """
    if not field_path:
        return 'parsed'

    # Regex for valid SQL identifiers (alphanumeric and underscore, starting with letter or underscore)
    identifier_pattern = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]*$')

    # Build field reference, using brackets for non-identifiers
    result = 'parsed'
    for component in field_path:
        if identifier_pattern.match(component):
            # Valid identifier - use dot notation
            result += f'.{component}'
        else:
            # Invalid identifier (e.g., contains hyphen) - use bracket notation
            # Escape single quotes in component
            component_escaped = component.replace("'", "''")
            result += f"['{component_escaped}']"

    return result


def _escape_sql_string(value: str) -> str:
    """Escape single quotes in SQL string literals.

    Args:
        value: String to escape

    Returns:
        Escaped string safe for SQL

    Examples:
        >>> _escape_sql_string("it's")
        "it''s"
    """
    return value.replace("'", "''")


def _query_with_polars(
    parquet_path: Path,
    filter_node: FilterNode | None,
    limit: int | None,
) -> Iterator[dict[str, Any]]:
    """Execute query using Polars backend.

    Args:
        parquet_path: Parquet file path
        filter_node: Filter AST
        limit: Result limit

    Yields:
        Dict for each matching row

    Raises:
        ValueError: If query fails
    """
    try:
        # Create lazy scan
        lf = pl.scan_parquet(str(parquet_path))

        # Apply filter if specified
        if filter_node is not None and filter_node.node_type != FilterNodeType.MATCH_ALL:
            filter_expr = _build_polars_filter_expr(filter_node)
            lf = lf.filter(filter_expr)

        # Apply limit if specified
        if limit is not None:
            lf = lf.limit(limit)

        # Collect with streaming engine
        try:
            # Try new engine parameter (Polars >= 1.25.0)
            df = lf.collect(engine='streaming')
        except TypeError:
            # Fall back to deprecated streaming parameter for older Polars versions
            df = lf.collect(streaming=True)  # type: ignore[call-overload]

        # Iterate through rows and yield as dicts
        yield from df.iter_rows(named=True)

    except pl.exceptions.PolarsError as e:
        msg = f'Polars query failed: {e}'
        raise ValueError(msg) from e


def _build_polars_filter_expr(node: FilterNode) -> pl.Expr:
    """Translate FilterNode to a Polars filter expression.

    Returns:
        Polars expression that evaluates the node.

    Raises:
        ValueError: If the node type is unsupported or malformed.
    """
    try:
        builder = _POLARS_BUILDERS[node.node_type]
    except KeyError as err:
        msg = f'Unsupported filter node type: {node.node_type}'
        raise ValueError(msg) from err

    return builder(node)


def _polars_expr_text_search(node: FilterNode) -> pl.Expr:
    value = (node.value or '').lower()
    return pl.col('message').str.to_lowercase().str.contains(value, literal=True)


def _polars_expr_exact_phrase(node: FilterNode) -> pl.Expr:
    value = (node.value or '').lower()
    return pl.col('message').str.to_lowercase().str.contains(value, literal=True)


def _polars_expr_regex(node: FilterNode) -> pl.Expr:
    return pl.col('message').str.contains(node.value or '', literal=False)


def _polars_expr_json_equals(node: FilterNode) -> pl.Expr:
    field_expr = _build_polars_field_reference(node.field_path or [])
    return pl.when(pl.col('parsed').is_not_null()).then(field_expr == node.value).otherwise(pl.lit(False))  # ruff: ignore[boolean-positional-value-in-call]


def _polars_expr_json_not_equals(node: FilterNode) -> pl.Expr:
    field_expr = _build_polars_field_reference(node.field_path or [])
    return pl.when(pl.col('parsed').is_not_null()).then(field_expr != node.value).otherwise(pl.lit(False))  # ruff: ignore[boolean-positional-value-in-call]


def _polars_expr_json_exists(node: FilterNode) -> pl.Expr:
    field_expr = _build_polars_field_reference(node.field_path or [])
    return pl.when(pl.col('parsed').is_not_null()).then(field_expr.is_not_null()).otherwise(pl.lit(False))  # ruff: ignore[boolean-positional-value-in-call]


def _polars_expr_json_numeric(node: FilterNode) -> pl.Expr:
    field_expr = _build_polars_field_reference(node.field_path or []).cast(pl.Float64)
    operator = node.operator or '='
    value = float(node.value or '0')

    comparison = None
    if operator == '=':
        comparison = field_expr == value
    elif operator == '!=':
        comparison = field_expr != value
    elif operator == '>':
        comparison = field_expr > value
    elif operator == '<':
        comparison = field_expr < value
    elif operator == '>=':
        comparison = field_expr >= value
    elif operator == '<=':
        comparison = field_expr <= value
    else:
        msg = f'Unsupported numeric operator: {operator}'
        raise ValueError(msg)

    return pl.when(pl.col('parsed').is_not_null()).then(comparison).otherwise(pl.lit(False))  # ruff: ignore[boolean-positional-value-in-call]


def _polars_expr_json_regex(node: FilterNode) -> pl.Expr:
    field_expr = _build_polars_field_reference(node.field_path or [])
    regex_match = field_expr.cast(pl.Utf8).str.contains(node.value or '', literal=False)
    return pl.when(pl.col('parsed').is_not_null()).then(regex_match).otherwise(pl.lit(False))  # ruff: ignore[boolean-positional-value-in-call]


def _polars_expr_and(node: FilterNode) -> pl.Expr:
    children = node.children or []
    if not children:
        msg = 'AND node must have children'
        raise ValueError(msg)

    expr = _build_polars_filter_expr(children[0])
    for child in children[1:]:
        expr &= _build_polars_filter_expr(child)
    return expr


def _polars_expr_or(node: FilterNode) -> pl.Expr:
    children = node.children or []
    if not children:
        msg = 'OR node must have children'
        raise ValueError(msg)

    expr = _build_polars_filter_expr(children[0])
    for child in children[1:]:
        expr |= _build_polars_filter_expr(child)
    return expr


def _polars_expr_not(node: FilterNode) -> pl.Expr:
    children = node.children or []
    if len(children) != 1:
        msg = 'NOT node must have exactly one child'
        raise ValueError(msg)
    return ~_build_polars_filter_expr(children[0])


PolarsExprBuilder = Callable[[FilterNode], pl.Expr]

_POLARS_BUILDERS: dict[FilterNodeType, PolarsExprBuilder] = {
    FilterNodeType.TEXT_SEARCH: _polars_expr_text_search,
    FilterNodeType.EXACT_PHRASE: _polars_expr_exact_phrase,
    FilterNodeType.REGEX: _polars_expr_regex,
    FilterNodeType.JSON_FIELD_EQUALS: _polars_expr_json_equals,
    FilterNodeType.JSON_FIELD_NOT_EQUALS: _polars_expr_json_not_equals,
    FilterNodeType.JSON_FIELD_EXISTS: _polars_expr_json_exists,
    FilterNodeType.JSON_FIELD_NUMERIC: _polars_expr_json_numeric,
    FilterNodeType.JSON_FIELD_REGEX: _polars_expr_json_regex,
    FilterNodeType.AND: _polars_expr_and,
    FilterNodeType.OR: _polars_expr_or,
    FilterNodeType.NOT: _polars_expr_not,
}


def _build_polars_field_reference(field_path: list[str]) -> pl.Expr:
    """Build Polars struct field reference from field path.

    Args:
        field_path: List of field components

    Returns:
        Polars expression for accessing the struct field

    Examples:
        >>> _build_polars_field_reference(['level'])
        # Returns: pl.col('parsed').struct.field('level')

        >>> _build_polars_field_reference(['context', 'user', 'id'])
        # Returns: pl.col('parsed').struct.field('context').struct.field('user').struct.field('id')
    """
    if not field_path:
        return pl.col('parsed')

    # Start with parsed column
    expr = pl.col('parsed')

    # Chain struct.field() calls for each level
    for field in field_path:
        expr = expr.struct.field(field)

    return expr


def _dict_to_log_event(row: Mapping[str, Any]) -> LogEvent:
    """Convert query result dict to LogEvent instance.

    Args:
        row: Row dict from query result

    Returns:
        Reconstructed LogEvent instance

    Raises:
        ValueError: If row is missing required fields
        TypeError: If timestamp cannot be coerced to datetime
    """
    # Parse ISO timestamp strings back to datetime
    timestamp_str = row.get('timestamp')
    if timestamp_str is None:
        msg = 'Row missing required field: timestamp'
        raise ValueError(msg)

    if isinstance(timestamp_str, str):
        timestamp = datetime.fromisoformat(timestamp_str)
    elif isinstance(timestamp_str, datetime):
        timestamp = timestamp_str
    else:
        msg = f'Invalid timestamp type: {type(timestamp_str)}'
        raise TypeError(msg)

    # Handle optional ingestion_time
    ingestion_time = None
    ingestion_time_value = row.get('ingestion_time')
    if ingestion_time_value is not None:
        if isinstance(ingestion_time_value, str):
            ingestion_time = datetime.fromisoformat(ingestion_time_value)
        elif isinstance(ingestion_time_value, datetime):
            ingestion_time = ingestion_time_value

    return LogEvent(
        log_group=str(row.get('log_group', '')),
        log_stream=str(row.get('log_stream', '')),
        timestamp=timestamp,
        message=str(row.get('message', '')),
        event_id=str(row.get('event_id', '')),
        ingestion_time=ingestion_time,
    )


def query_parquet_file_to_log_events(
    parquet_path: Path,
    filter_node: FilterNode | None = None,
    *,
    backend: QueryBackend = QueryBackend.AUTO,
    limit: int | None = None,
) -> Iterator[LogEvent]:
    """Query Parquet file and return LogEvent instances.

    Convenience wrapper around query_parquet_file that converts
    result dicts to LogEvent instances.

    Args:
        parquet_path: Path to Parquet file to query
        filter_node: Parsed filter AST (None = return all records)
        backend: Backend to use (DUCKDB, POLARS, or AUTO)
        limit: Maximum number of results to return (None = no limit)

    Yields:
        LogEvent instances for matching records

    Examples:
        >>> from tail_cw.query.parser import parse_filter_pattern
        >>> filter_node = parse_filter_pattern('{ $.level = "ERROR" }')
        >>> for event in query_parquet_file_to_log_events(Path('logs.parquet'), filter_node):
        ...     print(f"{event.timestamp}: {event.message}")
    """
    for row in query_parquet_file(
        parquet_path,
        filter_node,
        backend=backend,
        limit=limit,
    ):
        yield _dict_to_log_event(row)


def query_parquet_files_to_log_events(
    parquet_paths: Sequence[Path],
    filter_node: FilterNode | None = None,
    *,
    backend: QueryBackend = QueryBackend.AUTO,
    limit: int | None = None,
) -> Iterator[LogEvent]:
    """Query several Parquet files and yield their events merged by timestamp.

    Each file holds one log group, so a multi-group search reads them
    independently and interleaves the results. ``limit`` caps the merged
    output rather than each file, and is also applied per file so a single
    busy group cannot exhaust memory before the merge.

    Yields:
        LogEvent instances in ascending timestamp order across all files
    """
    streams = [
        query_parquet_file_to_log_events(path, filter_node, backend=backend, limit=limit) for path in parquet_paths
    ]
    merged = heapq.merge(*streams, key=lambda event: event.timestamp)
    yield from islice(merged, limit) if limit is not None else merged


def benchmark_backends(
    parquet_path: Path,
    filter_node: FilterNode | None = None,
    iterations: int = 3,
) -> dict[str, float]:
    """Benchmark query performance across backends.

    Useful for performance tuning and understanding which backend
    is optimal for specific query patterns.

    Args:
        parquet_path: Parquet file to benchmark
        filter_node: Filter to test (None = full scan)
        iterations: Number of iterations for averaging

    Returns:
        Dict mapping backend name to average query time in seconds
        Example: {'duckdb': 0.123, 'polars': 0.098}

    Examples:
        >>> from tail_cw.query.parser import parse_filter_pattern
        >>> filter_node = parse_filter_pattern('ERROR')
        >>> times = benchmark_backends(Path('logs.parquet'), filter_node, iterations=5)
        >>> print(f"DuckDB: {times['duckdb']:.3f}s, Polars: {times['polars']:.3f}s")
    """
    results: dict[str, float] = {}

    for backend in [QueryBackend.DUCKDB, QueryBackend.POLARS]:
        total_time = 0.0

        for _ in range(iterations):
            start = time.perf_counter()

            # Execute query and consume iterator to ensure full execution
            for _ in query_parquet_file(parquet_path, filter_node, backend=backend):
                pass

            end = time.perf_counter()
            total_time += end - start

        avg_time = total_time / iterations
        results[backend.value] = avg_time

    return results
