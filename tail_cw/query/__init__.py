"""Query engine for local log search.

This module provides a query abstraction layer supporting both DuckDB and
Polars backends for filtering cached logs. It implements a CloudWatch-style
filter pattern parser and extends it with key:value syntax for parsed JSONL
fields. The engine automatically selects the most performant backend based
on query characteristics.
"""
