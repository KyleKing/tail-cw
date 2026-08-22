"""Local log cache with Parquet storage.

This module provides disk-based caching of CloudWatch logs using DiskCache
for metadata management and Parquet files for efficient columnar storage.
It handles JSONL parsing (detected by leading '{' after timestamp) using
Polars, implements TTL and FIFO eviction policies, and uses ZSTD compression
for optimal storage efficiency.
"""

from tail_cw.cache.storage import (
    LogCache,
    generate_cache_key,
    is_jsonl_message,
    read_parquet_to_log_events,
    write_log_events_to_parquet,
)
from tail_cw.cache.window import Segment, plan_segments

__all__ = [
    'LogCache',
    'Segment',
    'generate_cache_key',
    'is_jsonl_message',
    'plan_segments',
    'read_parquet_to_log_events',
    'write_log_events_to_parquet',
]
