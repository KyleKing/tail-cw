"""Local query engine: the filter DSL, the dual backends, and trace grouping.

Import the modules directly. A facade here would load DuckDB and Polars for a
caller that only wanted to parse a filter.
"""
