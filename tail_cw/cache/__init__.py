"""Local log cache with Parquet storage.

This module provides disk-based caching of CloudWatch logs using DiskCache
for metadata management and Parquet files for efficient columnar storage.
It handles JSONL parsing (detected by leading '{' after timestamp) using
Polars, implements TTL and FIFO eviction policies, and uses ZSTD compression
for optimal storage efficiency.
"""
