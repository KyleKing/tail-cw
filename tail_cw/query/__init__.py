"""Query engine for local log search.

This module provides a query abstraction layer supporting both DuckDB and
Polars backends for filtering cached logs. It implements a CloudWatch-style
filter pattern parser and extends it with key:value syntax for parsed JSONL
fields. The engine automatically selects the most performant backend based
on query characteristics.
"""

from tail_cw.query.engine import (
    QueryBackend,
    benchmark_backends,
    query_parquet_file,
    query_parquet_file_to_log_events,
    query_parquet_files_to_log_events,
)
from tail_cw.query.parser import (
    FilterNode,
    FilterNodeType,
    combine_filters,
    parse_extended_filter,
    parse_filter_pattern,
)
from tail_cw.query.trace import (
    DEFAULT_TRACE_ID_FIELDS,
    TraceGroup,
    TraceSpan,
    extract_trace_id_from_event,
    find_traces_with_errors,
    query_traces_from_parquet,
)

__all__ = [
    'DEFAULT_TRACE_ID_FIELDS',
    'FilterNode',
    'FilterNodeType',
    'QueryBackend',
    'TraceGroup',
    'TraceSpan',
    'benchmark_backends',
    'combine_filters',
    'extract_trace_id_from_event',
    'find_traces_with_errors',
    'parse_extended_filter',
    'parse_filter_pattern',
    'query_parquet_file',
    'query_parquet_file_to_log_events',
    'query_parquet_files_to_log_events',
    'query_traces_from_parquet',
]
