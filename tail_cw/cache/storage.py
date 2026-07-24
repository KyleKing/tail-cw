"""Cache storage module for CloudWatch Logs events.

Provides efficient caching of log events using Parquet files with ZSTD compression.
Supports TTL expiration and FIFO eviction policies via DiskCache metadata management.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import tempfile
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime
from operator import itemgetter
from pathlib import Path

import polars as pl
from diskcache import Cache

from tail_cw.aws.client import LogEvent

# Progress callback signature: current progress, total (or -1 when unknown), status message.
ProgressCallback = Callable[[int, int, str], None]

# Precompiled regex for detecting ISO8601/RFC3339 timestamps at start of message
# Matches formats like: 2025-01-01T12:00:00Z, 2025-01-01T12:00:00.123456+00:00
_TIMESTAMP_PREFIX_RE = re.compile(
    r'^\s*\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?\s*',
)


def generate_cache_key(
    log_group: str,
    start_time: datetime,
    end_time: datetime,
    filter_pattern: str | None = None,
    log_stream_names: list[str] | None = None,
    region_name: str | None = None,
    profile_name: str | None = None,
) -> str:
    """Generate a deterministic cache key from CloudWatch query parameters.

    Creates a compact, deterministic cache key by hashing the canonical JSON
    representation of query parameters. Uses BLAKE2b for fast, secure hashing
    with a personalization parameter for domain separation.

    The cache key format is versioned (v1) to allow future schema migrations
    without collision with old cache entries.

    Args:
        log_group: CloudWatch log group name.
        start_time: Start of time range (inclusive).
        end_time: End of time range (inclusive).
        filter_pattern: Optional CloudWatch filter pattern for server-side filtering.
        log_stream_names: Optional list of log stream names. Order is normalized
            for determinism.
        region_name: Optional AWS region name.
        profile_name: Optional AWS profile name. Included so results fetched
            with different profiles (potentially different accounts) do not
            collide in the cache.

    Returns:
        Cache key in format: cache:v1:{base64url_digest}
        Example: cache:v1:yH8aKp3mR5nQ7xW2vL9kJg

    Example:
        >>> from datetime import datetime, timezone
        >>> start = datetime(2025, 1, 1, tzinfo=timezone.utc)
        >>> end = datetime(2025, 1, 2, tzinfo=timezone.utc)
        >>> key1 = generate_cache_key('/aws/lambda/fn', start, end)
        >>> key2 = generate_cache_key('/aws/lambda/fn', start, end)
        >>> key1 == key2  # Deterministic
        True
    """
    # Create canonical representation
    canonical = {
        'log_group': log_group,
        'start_time': start_time.isoformat(),
        'end_time': end_time.isoformat(),
    }

    if filter_pattern is not None:
        canonical['filter_pattern'] = filter_pattern

    if log_stream_names is not None:
        # Sort for determinism regardless of input order
        canonical['log_stream_names'] = sorted(log_stream_names)  # type: ignore[assignment]

    if region_name is not None:
        canonical['region_name'] = region_name

    if profile_name is not None:
        canonical['profile_name'] = profile_name

    # Create stable JSON representation
    json_bytes = json.dumps(
        canonical,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')

    # Hash with BLAKE2b (fast, secure, compact)
    hasher = hashlib.blake2b(
        json_bytes,
        digest_size=16,  # 128-bit digest
        person=b'tail-cw:cache',  # Domain separation
    )

    # Encode as base64url (URL-safe, no padding)
    digest_b64 = base64.urlsafe_b64encode(hasher.digest()).decode('ascii').rstrip('=')

    return f'cache:v1:{digest_b64}'


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


def _log_events_to_ndjson_file(
    log_events: Iterable[LogEvent],
    output_path: Path,
    progress_callback: ProgressCallback | None = None,
) -> tuple[int, int]:
    """Write LogEvent instances to a temporary NDJSON file.

    Creates a newline-delimited JSON file where each line is a JSON object
    representing a LogEvent. If a message appears to be JSON, attempts to
    parse it and merge the parsed fields under a 'parsed' key.

    Args:
        log_events: Iterator of log events to write.
        output_path: Path where NDJSON file will be written.
        progress_callback: Optional callable invoked every 1000 events with
            the signature ``(current, total, status_message)``. The total value
            is ``-1`` because the full size is unknown while streaming input.

    Returns:
        Tuple of (total_events, jsonl_events) counts.

    Raises:
        OSError: If output file cannot be written.
    """
    total_events = 0
    jsonl_events = 0

    with output_path.open('w') as f:
        for event in log_events:
            total_events += 1

            if progress_callback and total_events % 1000 == 0:
                progress_callback(total_events, -1, 'Parsing JSONL...')

            # Create base record with all LogEvent fields
            record = {
                'log_group': event.log_group,
                'log_stream': event.log_stream,
                'timestamp': event.timestamp.isoformat(),
                'message': event.message,
                'event_id': event.event_id,
                'ingestion_time': (event.ingestion_time.isoformat() if event.ingestion_time is not None else None),
            }

            # Try to parse message as JSON if it looks like JSON
            if is_jsonl_message(event.message):
                try:
                    parsed = json.loads(event.message)
                    record['parsed'] = parsed
                    jsonl_events += 1
                except json.JSONDecodeError:
                    # Malformed JSON - keep raw message
                    pass

            # Write as compact JSON line
            f.write(json.dumps(record, separators=(',', ':')) + '\n')

    return total_events, jsonl_events


def write_log_events_to_parquet(
    log_events: Iterable[LogEvent],
    output_path: Path,
    compression_level: int = 3,
    row_group_size: int = 100_000,
    infer_schema_length: int = 1000,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, int]:
    """Convert LogEvent instances to a compressed Parquet file.

    Writes log events to a Parquet file with ZSTD compression for efficient
    storage and fast querying. Uses streaming processing via temporary NDJSON
    file to avoid loading all events into memory.

    The Parquet schema is inferred from the NDJSON data and includes:
    - Base columns: log_group, log_stream, timestamp, message, event_id, ingestion_time
    - Optional 'parsed' struct column when JSONL messages are detected

    Args:
        log_events: Iterator of log events to store.
        output_path: Path where Parquet file will be written.
        compression_level: ZSTD compression level (1-22). Higher = better
            compression but slower. Default 3 is a good balance.
        row_group_size: Number of rows per Parquet row group. Larger values
            improve scan performance at the cost of memory usage.
        infer_schema_length: Number of rows read while inferring the schema
            from NDJSON input.
        progress_callback: Optional callable invoked during conversion with
            ``(current, total, status_message)``.

    Returns:
        Statistics dict with keys:
            - total_events: Total number of events written
            - jsonl_events: Number of events with successfully parsed JSON messages
            - file_size_bytes: Size of the Parquet file in bytes

    Raises:
        ValueError: If input is invalid or Parquet conversion fails.
        OSError: If output file cannot be written.

    Example:
        >>> from tail_cw.aws.client import LogEvent
        >>> from datetime import datetime, timezone
        >>> from pathlib import Path
        >>> events = [
        ...     LogEvent(
        ...         log_group='/aws/lambda/fn',
        ...         log_stream='2025/01/01/stream',
        ...         timestamp=datetime.now(tz=timezone.utc),
        ...         message='Test message',
        ...         event_id='event-1',
        ...         ingestion_time=None,
        ...     )
        ... ]
        >>> stats = write_log_events_to_parquet(events, Path('output.parquet'))
        >>> stats['total_events']
        1
    """
    # Create temporary NDJSON file
    temp_file = None
    try:
        with tempfile.NamedTemporaryFile(
            mode='w',
            suffix='.jsonl',
            delete=False,
        ) as f:
            temp_file = Path(f.name)

        # Write events to NDJSON
        total_events, jsonl_events = _log_events_to_ndjson_file(
            log_events,
            temp_file,
            progress_callback=progress_callback,
        )

        if total_events == 0:
            # Clean up and raise error for empty input
            temp_file.unlink()
            msg = 'Cannot create Parquet file from empty log events'
            raise ValueError(msg)

        if progress_callback:
            progress_callback(total_events, total_events, 'Converting to Parquet...')

        # Convert NDJSON to Parquet using Polars
        # Use scan_ndjson for lazy loading with schema inference
        (
            pl.scan_ndjson(str(temp_file), infer_schema_length=infer_schema_length).sink_parquet(
                str(output_path),
                compression='zstd',
                compression_level=compression_level,
                row_group_size=row_group_size,
            )
        )

        # Get file size
        file_size_bytes = output_path.stat().st_size

        return {
            'total_events': total_events,
            'jsonl_events': jsonl_events,
            'file_size_bytes': file_size_bytes,
        }

    finally:
        # Clean up temporary file
        if temp_file is not None and temp_file.exists():
            temp_file.unlink()


def read_parquet_to_log_events(parquet_path: Path) -> Iterator[LogEvent]:
    """Read a Parquet file back to LogEvent instances.

    Performs the inverse operation of write_log_events_to_parquet,
    reconstructing LogEvent objects from the Parquet schema. Uses
    streaming to avoid loading the entire file into memory.

    Args:
        parquet_path: Path to Parquet file created by write_log_events_to_parquet.

    Yields:
        LogEvent instances for each row in the Parquet file.

    Raises:
        FileNotFoundError: If Parquet file doesn't exist.
        ValueError: If Parquet file has invalid schema or data.

    Example:
        >>> events = list(read_parquet_to_log_events(Path('output.parquet')))
        >>> events[0].log_group
        '/aws/lambda/fn'
    """
    if not parquet_path.exists():
        msg = f'Parquet file not found: {parquet_path}'
        raise FileNotFoundError(msg)

    # Use scan_parquet with streaming engine to avoid loading entire file
    # Process in batches to balance memory usage and iteration overhead
    lazy_df = pl.scan_parquet(str(parquet_path))

    # Collect with streaming engine for memory-efficient processing
    try:
        # Try new engine parameter (Polars >= 1.25.0)
        df_iter = lazy_df.collect(engine='streaming')
    except TypeError:
        # Fall back to deprecated streaming parameter for older Polars versions
        df_iter = lazy_df.collect(streaming=True)  # type: ignore[call-arg, call-overload]

    # Iterate through rows and reconstruct LogEvent instances
    for row in df_iter.iter_rows(named=True):
        # Parse ISO timestamp strings back to datetime objects
        timestamp = datetime.fromisoformat(row['timestamp'])

        # Handle optional ingestion_time
        ingestion_time = None
        if row.get('ingestion_time') is not None:
            ingestion_time = datetime.fromisoformat(row['ingestion_time'])

        yield LogEvent(
            log_group=row['log_group'],
            log_stream=row['log_stream'],
            timestamp=timestamp,
            message=row['message'],
            event_id=row['event_id'],
            ingestion_time=ingestion_time,
        )


class LogCache:
    """Cache manager for CloudWatch Logs events with TTL and FIFO eviction.

    Stores log events in compressed Parquet files with DiskCache metadata
    management. Supports time-to-live (TTL) expiration and least-recently-stored
    (FIFO) eviction when size limits are reached.

    The cache uses a two-tier storage approach:
    1. DiskCache stores metadata (cache key -> Parquet file path mappings)
    2. Parquet files store the actual log events with ZSTD compression

    When cache entries are evicted (via TTL or FIFO), both the metadata and
    the corresponding Parquet files are automatically cleaned up.

    Attributes:
        _cache_dir: Base directory for cache storage.
        _parquet_dir: Subdirectory containing Parquet files.
        _metadata: DiskCache instance for metadata management.
        _default_ttl: Default TTL in seconds for cache entries (None = no expiration).

    Example:
        >>> from pathlib import Path
        >>> from datetime import datetime, timezone, timedelta
        >>> from tail_cw.aws.client import LogEvent
        >>> from tail_cw.cache import LogCache, generate_cache_key
        >>> # Create cache with 1GB limit and 1-hour default TTL
        >>> cache_dir = Path('/tmp/my-cache')
        >>> with LogCache(cache_dir, size_limit_mb=1000, default_ttl_seconds=3600) as cache:
        ...     # Generate cache key
        ...     start = datetime.now(tz=timezone.utc) - timedelta(hours=1)
        ...     end = datetime.now(tz=timezone.utc)
        ...     key = generate_cache_key('/aws/lambda/fn', start, end)
        ...
        ...     # Write events
        ...     events = [LogEvent(...), ...]
        ...     stats = cache.write(events, key)
        ...
        ...     # Read back later
        ...     cached_events = list(cache.read(key))
    """

    def __init__(
        self,
        cache_dir: Path,
        size_limit_mb: int = 1000,
        default_ttl_seconds: int | None = None,
        eviction_policy: str = 'least-recently-stored',
        compression_level: int = 3,
        row_group_size: int = 100_000,
        infer_schema_length: int = 1000,
    ) -> None:
        """Initialize LogCache with specified configuration.

        Args:
            cache_dir: Directory for cache storage. Created if it doesn't exist.
            size_limit_mb: Maximum cache size in MB. Default 1000 (1GB).
            default_ttl_seconds: Default TTL for cache entries in seconds.
                None means no expiration. Default None.
            eviction_policy: DiskCache eviction policy. Default 'least-recently-stored'
                for FIFO behavior. See DiskCache docs for other options.
            compression_level: Default ZSTD compression level applied when
                writing Parquet files.
            row_group_size: Default Parquet row group size used during writes.
            infer_schema_length: Number of rows scanned when inferring schemas.

        Raises:
            OSError: If cache directory cannot be created.
        """
        self._cache_dir = cache_dir
        self._parquet_dir = cache_dir / 'parquet'
        self._default_ttl = default_ttl_seconds
        self._size_limit_bytes = size_limit_mb * 1024 * 1024
        self._compression_level = compression_level
        self._row_group_size = row_group_size
        self._infer_schema_length = infer_schema_length

        # Create directories
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._parquet_dir.mkdir(parents=True, exist_ok=True)

        # Initialize DiskCache with size limit and eviction policy
        # Size limit is in bytes
        self._metadata = Cache(
            str(self._cache_dir / 'metadata'),
            size_limit=self._size_limit_bytes,
            eviction_policy=eviction_policy,
        )

    def _cleanup_orphaned_files(self) -> int:
        """Clean up Parquet files not referenced by any metadata entry.

        Returns:
            Number of orphaned files deleted.
        """
        # Find all files referenced in metadata
        referenced_files = set()
        for cache_key in self._metadata.iterkeys():  # type: ignore[attr-defined]
            metadata_value = self._metadata.get(cache_key)
            if metadata_value is not None:
                # Handle both old string format and new tuple format
                path_str = str(metadata_value[0]) if isinstance(metadata_value, tuple) else str(metadata_value)
                referenced_files.add(Path(path_str))

        # Delete orphaned Parquet files
        cleaned_count = 0
        for parquet_file in self._parquet_dir.glob('*.parquet'):
            if parquet_file not in referenced_files:
                parquet_file.unlink()
                cleaned_count += 1

        return cleaned_count

    def _enforce_parquet_size_limit(self) -> int:
        """Enforce Parquet directory size limit via FIFO eviction.

        Computes total size of Parquet files and deletes oldest files
        (by creation time) until under the size limit. Also removes
        corresponding metadata entries.

        Returns:
            Number of files evicted.
        """
        # Collect all Parquet files with their sizes and creation times
        parquet_files = []
        total_size = 0
        for parquet_file in self._parquet_dir.glob('*.parquet'):
            stat = parquet_file.stat()
            size = stat.st_size
            # Use birthtime if available, otherwise ctime
            ctime = getattr(stat, 'st_birthtime', stat.st_ctime)
            parquet_files.append((parquet_file, size, ctime))
            total_size += size

        # Check if we're over the limit
        if total_size <= self._size_limit_bytes:
            return 0

        # Sort by creation time (oldest first) for FIFO
        parquet_files.sort(key=itemgetter(2))

        # Delete oldest files until under limit
        evicted_count = 0
        for parquet_file, size, _ctime in parquet_files:
            if total_size <= self._size_limit_bytes:
                break

            # Find and delete corresponding metadata entries
            for cache_key in list(self._metadata.iterkeys()):  # type: ignore[attr-defined]
                metadata_value = self._metadata.get(cache_key)
                if metadata_value is not None:
                    # Handle both old string format and new tuple format
                    path_str = str(metadata_value[0]) if isinstance(metadata_value, tuple) else str(metadata_value)

                    if Path(path_str) == parquet_file:
                        self._metadata.delete(cache_key)

            # Delete the Parquet file
            parquet_file.unlink()
            total_size -= size
            evicted_count += 1

        return evicted_count

    def write(
        self,
        log_events: Iterable[LogEvent],
        cache_key: str,
        ttl_seconds: int | None = None,
        compression_level: int | None = None,
        row_group_size: int | None = None,
        infer_schema_length: int | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> dict[str, int]:
        """Write log events to cache.

        Args:
            log_events: Iterator of log events to cache.
            cache_key: Cache key to store under. Use generate_cache_key() to create.
            ttl_seconds: TTL for this entry in seconds. If None, uses default_ttl_seconds.
                Pass None explicitly to override default and use no expiration.
            compression_level: Optional override for compression level used during
                this write.
            row_group_size: Optional override for Parquet row group size.
            infer_schema_length: Optional override for schema inference length.
            progress_callback: Optional callable notified of progress updates.

        Returns:
            Statistics dict from write_log_events_to_parquet().

        Raises:
            ValueError: If log_events is empty or invalid.
            OSError: If Parquet file cannot be written.
        """
        # Generate Parquet filename from cache key (sanitize for filesystem)
        # Use the full cache key as filename (already URL-safe base64)
        parquet_filename = cache_key.replace(':', '_').replace('/', '_') + '.parquet'
        parquet_path = self._parquet_dir / parquet_filename

        effective_compression = compression_level if compression_level is not None else self._compression_level
        effective_row_group_size = row_group_size if row_group_size is not None else self._row_group_size
        effective_infer_schema_length = (
            infer_schema_length if infer_schema_length is not None else self._infer_schema_length
        )

        # Write events to Parquet
        stats = write_log_events_to_parquet(
            log_events,
            parquet_path,
            compression_level=effective_compression,
            row_group_size=effective_row_group_size,
            infer_schema_length=effective_infer_schema_length,
            progress_callback=progress_callback,
        )

        # Store metadata in DiskCache with file size for efficient size tracking
        ttl = ttl_seconds if ttl_seconds is not None else self._default_ttl
        metadata_value = (str(parquet_path), stats['file_size_bytes'])
        self._metadata.set(
            cache_key,
            metadata_value,
            expire=ttl,
        )

        # Trigger DiskCache expiration and culling to enforce TTL and size limits
        self._metadata.expire()
        # Trigger cull to apply DiskCache eviction policy
        # Note: cull() returns the number of entries evicted
        self._metadata.cull()

        # Clean up orphaned Parquet files after metadata changes
        self._cleanup_orphaned_files()

        # Enforce Parquet directory size limit
        self._enforce_parquet_size_limit()

        return stats

    def read(self, cache_key: str) -> Iterator[LogEvent]:
        """Read log events from cache.

        Args:
            cache_key: Cache key to read from.

        Returns:
            Iterator of LogEvent instances. Returns empty iterator if key
            doesn't exist or file is missing.

        Example:
            >>> events = list(cache.read('cache:v1:abc123'))
            >>> for event in events:
            ...     print(event.message)
        """
        # Get Parquet path from metadata
        metadata_value = self._metadata.get(cache_key)

        if metadata_value is None:
            return iter(())

        # Handle both old string format and new tuple format
        parquet_path_str = str(metadata_value[0]) if isinstance(metadata_value, tuple) else str(metadata_value)

        parquet_path = Path(parquet_path_str)

        # Check if file exists (may have been deleted externally)
        if not parquet_path.exists():
            # Clean up stale metadata
            self._metadata.delete(cache_key)
            return iter(())

        # Read and return events
        return read_parquet_to_log_events(parquet_path)

    def exists(self, cache_key: str) -> bool:
        """Check if a cache key exists.

        Args:
            cache_key: Cache key to check.

        Returns:
            True if key exists and is valid, False otherwise.
        """
        return self.get_parquet_path(cache_key) is not None

    def get_parquet_path(self, cache_key: str) -> Path | None:
        """Return the Parquet file path for a cache key.

        Args:
            cache_key: Cache key to look up.

        Returns:
            Path to the cached Parquet file, or None when the key is missing,
            expired, or the file was deleted externally (stale metadata is
            cleaned up).
        """
        metadata_value = self._metadata.get(cache_key)

        if metadata_value is None:
            return None

        # Handle both old string format and new tuple format
        parquet_path_str = str(metadata_value[0]) if isinstance(metadata_value, tuple) else str(metadata_value)

        parquet_path = Path(parquet_path_str)
        if not parquet_path.exists():
            # Clean up stale metadata
            self._metadata.delete(cache_key)
            return None

        return parquet_path

    def evict_expired(self) -> int:
        """Manually trigger expiration of TTL entries and clean up orphaned files.

        Removes expired cache entries and deletes Parquet files that are no longer
        referenced by any metadata entry. Also enforces Parquet directory size limit.

        Returns:
            Number of orphaned Parquet files cleaned up.

        Example:
            >>> cache.evict_expired()
            3  # Cleaned up 3 orphaned files
        """
        # Trigger DiskCache expiration to remove TTL-expired entries
        self._metadata.expire()

        # Trigger cull to apply DiskCache eviction policy
        self._metadata.cull()

        # Clean up orphaned Parquet files after metadata changes
        cleaned_count = self._cleanup_orphaned_files()

        # Enforce Parquet directory size limit
        self._enforce_parquet_size_limit()

        return cleaned_count

    def clear(self) -> None:
        """Clear all cache entries and delete all Parquet files.

        Example:
            >>> cache.clear()
        """
        # Clear all metadata
        self._metadata.clear()

        # Delete all Parquet files
        for parquet_file in self._parquet_dir.glob('*.parquet'):
            parquet_file.unlink()

    def close(self) -> None:
        """Close the DiskCache instance.

        Should be called when done using the cache to ensure resources are released.
        Prefer using the context manager (with statement) instead.
        """
        self._metadata.close()

    def __enter__(self) -> LogCache:
        """Context manager entry.

        Returns:
            The LogCache instance (self).
        """
        return self

    def __exit__(self, *args: object) -> None:
        """Context manager exit."""
        self.close()
