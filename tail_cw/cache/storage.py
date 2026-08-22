"""Cache storage module for CloudWatch Logs events.

Provides efficient caching of log events using Parquet files with ZSTD compression.
Supports TTL expiration and FIFO eviction policies via DiskCache metadata management.
"""

from __future__ import annotations

import base64
import hashlib
import json
import tempfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from operator import itemgetter
from pathlib import Path
from threading import Lock
from typing import Any

import polars as pl
from diskcache import Cache, JSONDisk

from tail_cw.aws.events import LogEvent
from tail_cw.cache.records import is_jsonl_message, readable_message
from tail_cw.progress import TOTAL_UNKNOWN, ProgressCallback

TtlSeconds = int | float
"""Cache TTL in seconds. Explicit union because beartype does not widen int to float."""

CACHE_KEY_PREFIX = 'cache:v2'
"""Prefix of every log-event cache key.

Bumped when the stored Parquet schema changes, so an older file is never read
back under the current schema. :meth:`LogCache._prune_superseded_entries`
reclaims the files the previous version left behind.
"""

METADATA_DIRNAME = 'metadata-v2'
"""Metadata store directory.

Versioned because v2 serializes with JSON instead of pickle. A pickle-era store
reads back as empty under JSONDisk, which would make every cached Parquet file
look orphaned and get swept on the first write. A new directory makes the format
change explicit: the old cache is simply cold, and its files are reclaimed by the
normal orphan sweep.
"""


# Precompiled regex for detecting ISO8601/RFC3339 timestamps at start of message
# Matches formats like: 2025-01-01T12:00:00Z, 2025-01-01T12:00:00.123456+00:00
def generate_cache_key(
    log_group: str,
    start_time: datetime,
    end_time: datetime,
    log_stream_names: list[str] | None = None,
    region_name: str | None = None,
    profile_name: str | None = None,
) -> str:
    """Generate a deterministic cache key from CloudWatch query parameters.

    Hashes the canonical JSON of the query parameters with BLAKE2b, personalized
    for domain separation. The ``cache:v2`` prefix keeps v1 files (which carried
    ``event_id`` and a raw duplicate of every JSON message) from being read back
    under the current schema.

    No filter is part of the key. Historical fetches always retrieve the whole
    window and filter locally, so one cached window serves every filter asked of
    it.

    Args:
        log_group: CloudWatch log group name.
        start_time: Start of the time range (inclusive).
        end_time: End of the time range (exclusive).
        log_stream_names: Optional list of log stream names. Order is normalized
            for determinism.
        region_name: Optional AWS region name.
        profile_name: Optional AWS profile name. Included so results fetched
            with different profiles (potentially different accounts) do not
            collide in the cache.

    Returns:
        Cache key in format: cache:v2:{base64url_digest}

    Example:
        >>> from datetime import datetime, timezone
        >>> start = datetime(2025, 1, 1, tzinfo=timezone.utc)
        >>> end = datetime(2025, 1, 2, tzinfo=timezone.utc)
        >>> generate_cache_key('/aws/lambda/fn', start, end) == generate_cache_key(
        ...     '/aws/lambda/fn', start, end
        ... )
        True
    """
    canonical: dict[str, Any] = {
        'log_group': log_group,
        'start_time': start_time.isoformat(),
        'end_time': end_time.isoformat(),
    }

    if log_stream_names is not None:
        canonical['log_stream_names'] = sorted(log_stream_names)

    if region_name is not None:
        canonical['region_name'] = region_name

    if profile_name is not None:
        canonical['profile_name'] = profile_name

    return _hash_canonical(canonical, prefix=CACHE_KEY_PREFIX)


def generate_preview_cache_key(
    log_group: str,
    *,
    window_seconds: int,
    region_name: str | None = None,
    profile_name: str | None = None,
) -> str:
    """Generate a deterministic cache key for a log group preview.

    Args:
        log_group: CloudWatch log group name.
        window_seconds: Length of the sampled window in seconds. Only the length
            matters, so a rolling window keeps hitting the same entry until its
            TTL expires.
        region_name: Optional AWS region name.
        profile_name: Optional AWS profile name. Included so previews fetched
            with different profiles do not collide.

    Returns:
        Cache key in format: preview:v1:{base64url_digest}
    """
    canonical: dict[str, Any] = {
        'log_group': log_group,
        'window_seconds': window_seconds,
    }

    if region_name is not None:
        canonical['region_name'] = region_name

    if profile_name is not None:
        canonical['profile_name'] = profile_name

    return _hash_canonical(canonical, prefix='preview:v1')


def _hash_canonical(canonical: dict[str, Any], *, prefix: str) -> str:
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

    return f'{prefix}:{digest_b64}'


def _metadata_path(metadata_value: Any) -> str:
    """Extract the Parquet path from a metadata entry.

    Entries are ``[path, size]`` today. JSON serialization turns the stored
    tuple into a list, and older caches hold a bare path string, so all three
    shapes have to read back.
    """
    if isinstance(metadata_value, (tuple, list)):
        return str(metadata_value[0])
    return str(metadata_value)


_encode = json.JSONEncoder(separators=(',', ':')).encode
"""Encode one value as compact JSON. Bound once, because it is called per field per event."""

_LINE_BREAKS = ('\n', '\r')
"""What must not appear in text spliced into an NDJSON line. A regex here cost 0.08s per 73k events."""


def _parse_jsonl_message(message: str) -> dict[str, Any] | None:
    """Return the message decoded as a JSON object, or None when it is not one."""
    if not is_jsonl_message(message):
        return None
    try:
        parsed = json.loads(message)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _log_events_to_ndjson_file(
    log_events: Iterable[LogEvent],
    output_path: Path,
    progress_callback: ProgressCallback | None = None,
) -> tuple[int, int]:
    """Write LogEvent instances to a temporary NDJSON file.

    Each line is one event. A message that decodes as a JSON object is spliced
    into the line as the ``parsed`` value verbatim, rather than being re-encoded
    from the dict the check produced: Polars decodes the file straight after, so
    re-encoding the payload is work nobody reads. Measured over 72,767 cached
    events that took the Python side of the write from 0.39s to 0.22s, against
    0.64s for the Polars half, which is where the real parse happens.

    The message is still decoded once, to prove it is a JSON object before its
    text is trusted as one. Skipping that check as well saves another 0.077s and
    costs the guarantee: one malformed line that starts with a brace would make
    the whole file unreadable rather than being stored as text.

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

    with output_path.open('w', encoding='utf-8') as handle:
        for event in log_events:
            total_events += 1

            if progress_callback and total_events % 1000 == 0:
                progress_callback(total_events, TOTAL_UNKNOWN, 'Parsing JSONL...')

            parsed = _parse_jsonl_message(event.message)
            if parsed is None:
                # The raw line is only stored when nothing else can reproduce it. For a
                # JSON line it duplicates ``parsed`` and costs 41% of the file.
                handle.write(_ndjson_line(event, 'message', _encode(event.message)))
                continue
            jsonl_events += 1
            # A pretty-printed payload is valid JSON and still cannot be spliced: its
            # newlines would end the NDJSON line early and make the file unreadable.
            payload = event.message if not _has_line_break(event.message) else _encode(parsed)
            handle.write(_ndjson_line(event, 'parsed', payload))

    return total_events, jsonl_events


def _has_line_break(text: str) -> bool:
    return any(character in text for character in _LINE_BREAKS)


def _ndjson_line(event: LogEvent, payload_field: str, payload_text: str) -> str:
    """Build one NDJSON line, splicing ``payload_text`` in as already-encoded JSON."""
    ingestion = 'null' if event.ingestion_time is None else _encode(event.ingestion_time.isoformat())
    return (
        f'{{"log_group":{_encode(event.log_group)},"log_stream":{_encode(event.log_stream)},'
        f'"timestamp":{_encode(event.timestamp.isoformat())},"ingestion_time":{ingestion},'
        f'"{payload_field}":{payload_text}}}\n'
    )


def write_log_events_to_parquet(
    log_events: Iterable[LogEvent],
    output_path: Path,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, int]:
    """Convert LogEvent instances to a compressed Parquet file.

    Streams through a temporary NDJSON file so no more than one event is held in
    Python at a time. The written schema is ``log_group``, ``log_stream``,
    ``timestamp`` and ``ingestion_time`` as UTC datetimes, ``message``, and a
    ``parsed`` struct, sorted by timestamp. ``message`` is populated only for
    lines that are not JSON objects; for the rest ``parsed`` is the record and
    :func:`read_parquet_to_log_events` rebuilds the text from it.

    Args:
        log_events: Iterator of log events to store.
        output_path: Path where the Parquet file will be written.
        progress_callback: Optional callable invoked during conversion with
            ``(current, total, status_message)``.

    Returns:
        Statistics dict with keys ``total_events``, ``jsonl_events``, and
        ``file_size_bytes``.

    Raises:
        ValueError: If there are no events to write.
        OSError: If the output file cannot be written.
    """
    temp_file = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            temp_file = Path(f.name)

        total_events, jsonl_events = _log_events_to_ndjson_file(
            log_events,
            temp_file,
            progress_callback=progress_callback,
        )

        if total_events == 0:
            temp_file.unlink()
            msg = 'Cannot create Parquet file from empty log events'
            raise ValueError(msg)

        if progress_callback:
            progress_callback(total_events, total_events, 'Converting to Parquet...')

        # The whole file is scanned to infer the schema. Sampling the first N rows is
        # unsound over arbitrary log payloads: a key that is null in the sample and a string
        # later panics the Parquet writer, an int-then-string key fails to parse, and a key
        # first appearing after the sample is silently dropped and becomes unqueryable.
        lazy = pl.scan_ndjson(str(temp_file), infer_schema_length=None)
        # maintain_order keeps events that share a millisecond in the order CloudWatch
        # returned them, which is the only ordering information they carry.
        _normalized_columns(lazy).sort('timestamp', maintain_order=True).sink_parquet(
            str(output_path),
            compression='zstd',
        )

        return {
            'total_events': total_events,
            'jsonl_events': jsonl_events,
            'file_size_bytes': output_path.stat().st_size,
        }

    finally:
        if temp_file is not None and temp_file.exists():
            temp_file.unlink()


def _normalized_columns(lazy: pl.LazyFrame) -> pl.LazyFrame:
    """Give the frame the full v2 schema with real datetime columns.

    Timestamps arrive as ISO strings, and ``message`` or ``parsed`` are absent
    entirely when every line in the batch went the other way, so both are
    materialized as typed nulls to keep one schema across every cached file.
    """
    schema = lazy.collect_schema()
    casts = [
        pl.col(name).str.to_datetime(time_zone='UTC', strict=False)
        for name in ('timestamp', 'ingestion_time')
        if schema.get(name) == pl.String
    ]
    fills = [
        pl.lit(None, dtype=pl.String).alias(name)
        for name in ('message', 'ingestion_time')
        if name not in schema or schema[name] == pl.Null
    ]
    return lazy.with_columns(*casts, *fills)


def read_parquet_to_log_events(parquet_path: Path) -> Iterator[LogEvent]:
    """Read a Parquet file written by :func:`write_log_events_to_parquet`.

    A JSON event's text is re-serialized from ``parsed`` rather than stored, so
    the message reads back as compact JSON with any key it never carried
    dropped. Byte-for-byte whitespace and key order from the original line are
    not preserved.

    Args:
        parquet_path: Path to a cached Parquet file.

    Yields:
        LogEvent instances in file order.

    Raises:
        FileNotFoundError: If the Parquet file does not exist.
    """
    if not parquet_path.exists():
        msg = f'Parquet file not found: {parquet_path}'
        raise FileNotFoundError(msg)

    frame = pl.scan_parquet(str(parquet_path)).collect(engine='streaming')
    for row in frame.iter_rows(named=True):
        yield LogEvent(
            log_group=row['log_group'],
            log_stream=row['log_stream'],
            timestamp=row['timestamp'],
            message=readable_message(row),
            ingestion_time=row.get('ingestion_time'),
        )


@dataclass(frozen=True)
class CacheStatus:
    """What the cache holds right now, for :func:`LogCache.status`.

    No hit rate and no per-group breakdown, because neither is knowable: nothing counts
    reads, and a cache key is a BLAKE2b hash of the query, so the log group it came from
    is not recoverable from it.

    Attributes:
        cache_dir: Root of the cache.
        files: Parquet files on disk.
        bytes_used: Their total size.
        bytes_limit: The configured ceiling that eviction enforces.
        oldest: Creation time of the earliest file, or None when empty.
        newest: Creation time of the latest file, or None when empty.
        entries: Metadata entries.
        stale_entries: Entries whose Parquet file is gone.
        orphan_files: Files no entry points at. Normally transient rather than a leak:
            every write sweeps them, so a non-zero count usually means a fetch is in
            flight or the last one did not finish.
        default_ttl_seconds: Configured TTL, or None when entries never expire.
    """

    cache_dir: Path
    files: int
    bytes_used: int
    bytes_limit: int
    oldest: datetime | None
    newest: datetime | None
    entries: int
    stale_entries: int
    orphan_files: int
    default_ttl_seconds: TtlSeconds | None

    @property
    def fraction_used(self) -> float:
        """Share of the limit in use, 0.0 when there is no limit to speak of."""
        return self.bytes_used / self.bytes_limit if self.bytes_limit > 0 else 0.0


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
        >>> from tail_cw.aws.events import LogEvent
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
        default_ttl_seconds: TtlSeconds | None = None,
        eviction_policy: str = 'least-recently-stored',
    ) -> None:
        """Initialize LogCache with specified configuration.

        Args:
            cache_dir: Directory for cache storage. Created if it doesn't exist.
            size_limit_mb: Maximum cache size in MB. Default 1000 (1GB).
            default_ttl_seconds: Default TTL for cache entries in seconds.
                None means no expiration. Default None.
            eviction_policy: DiskCache eviction policy. Default 'least-recently-stored'
                for FIFO behavior. See DiskCache docs for other options.

        Raises:
            OSError: If cache directory cannot be created.
        """
        self._cache_dir = cache_dir
        self._parquet_dir = cache_dir / 'parquet'
        self._default_ttl = default_ttl_seconds
        self._size_limit_bytes = size_limit_mb * 1024 * 1024
        self._inflight: set[Path] = set()
        self._inflight_lock = Lock()

        # Create directories
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._parquet_dir.mkdir(parents=True, exist_ok=True)

        # Initialize DiskCache with size limit and eviction policy
        # Size limit is in bytes
        # JSONDisk rather than the default pickle Disk: CVE-2025-69872 lets anyone
        # with write access to the cache directory run code when we read it back,
        # and it has no upstream fix. Everything stored here is plain data, so JSON
        # costs nothing and removes the deserialization gadget entirely.
        self._metadata = Cache(
            str(self._cache_dir / METADATA_DIRNAME),
            size_limit=self._size_limit_bytes,
            eviction_policy=eviction_policy,
            disk=JSONDisk,
            disk_compress_level=1,
        )
        self._prune_superseded_entries()

    def _prune_superseded_entries(self) -> int:
        """Delete entries written under an older cache schema, files included.

        They can never be read again, because the key prefix moved with the
        schema, so they would sit as dead weight until FIFO eviction reached them.

        Returns:
            Number of entries removed.
        """
        stale = [
            key
            for key in self._metadata.iterkeys()  # type: ignore[attr-defined]
            if isinstance(key, str) and key.startswith('cache:') and not key.startswith(f'{CACHE_KEY_PREFIX}:')
        ]
        for key in stale:
            metadata_value = self._metadata.get(key)
            if metadata_value is not None:
                Path(_metadata_path(metadata_value)).unlink(missing_ok=True)
            self._metadata.delete(key)
        return len(stale)

    def _cleanup_orphaned_files(self) -> int:
        """Clean up Parquet files not referenced by any metadata entry.

        Returns:
            Number of orphaned files deleted.
        """
        # The whole sweep holds the in-flight lock, which is what makes it safe
        # against a concurrent fan-out. A writer registers its path before the
        # file exists and clears that registration only after its metadata is
        # set, so under this lock every file on disk is either registered or
        # referenced. Snapshotting the two sets separately leaves a window where
        # a write completes between them and the file looks orphaned.
        with self._inflight_lock:
            inflight = set(self._inflight)
            referenced_files = set()
            for cache_key in self._metadata.iterkeys():  # type: ignore[attr-defined]
                metadata_value = self._metadata.get(cache_key)
                if metadata_value is not None:
                    referenced_files.add(Path(_metadata_path(metadata_value)))

            cleaned_count = 0
            for parquet_file in self._parquet_dir.glob('*.parquet'):
                if parquet_file not in referenced_files and parquet_file not in inflight:
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
        # Held for the whole pass for the same reason as the orphan sweep: a
        # concurrent writer must not have its file evicted between the scan and
        # the delete.
        evicted_count = 0
        with self._inflight_lock:
            inflight = set(self._inflight)

            parquet_files = []
            total_size = 0
            for parquet_file in self._parquet_dir.glob('*.parquet'):
                stat = parquet_file.stat()
                size = stat.st_size
                # Use birthtime if available, otherwise ctime
                ctime = getattr(stat, 'st_birthtime', stat.st_ctime)
                parquet_files.append((parquet_file, size, ctime))
                total_size += size

            if total_size <= self._size_limit_bytes:
                return 0

            # Oldest first, for FIFO eviction
            parquet_files.sort(key=itemgetter(2))

            for parquet_file, size, _ctime in parquet_files:
                if total_size <= self._size_limit_bytes:
                    break
                if parquet_file in inflight:
                    continue

                # Find and delete corresponding metadata entries
                for cache_key in list(self._metadata.iterkeys()):  # type: ignore[attr-defined]
                    metadata_value = self._metadata.get(cache_key)
                    if metadata_value is not None and Path(_metadata_path(metadata_value)) == parquet_file:
                        self._metadata.delete(cache_key)

                parquet_file.unlink()
                total_size -= size
                evicted_count += 1

        return evicted_count

    def status(self) -> CacheStatus:
        """Report what is on disk against the limit, without changing anything.

        Counts files rather than trusting the metadata, because eviction deletes a file
        and its entry separately and a crash between the two leaves one of each behind.
        """
        referenced: set[Path] = set()
        stale = 0
        entries = 0
        for cache_key in list(self._metadata.iterkeys()):  # type: ignore[attr-defined]
            metadata_value = self._metadata.get(cache_key)
            if metadata_value is None:
                continue
            entries += 1
            path = Path(_metadata_path(metadata_value))
            referenced.add(path)
            if not path.exists():
                stale += 1

        files = 0
        used = 0
        stamps: list[float] = []
        orphans = 0
        for parquet_file in self._parquet_dir.glob('*.parquet'):
            stat = parquet_file.stat()
            files += 1
            used += stat.st_size
            stamps.append(getattr(stat, 'st_birthtime', stat.st_ctime))
            if parquet_file not in referenced:
                orphans += 1

        return CacheStatus(
            cache_dir=self._cache_dir,
            files=files,
            bytes_used=used,
            bytes_limit=self._size_limit_bytes,
            oldest=datetime.fromtimestamp(min(stamps), tz=UTC) if stamps else None,
            newest=datetime.fromtimestamp(max(stamps), tz=UTC) if stamps else None,
            entries=entries,
            stale_entries=stale,
            orphan_files=orphans,
            default_ttl_seconds=self._default_ttl,
        )

    def write(
        self,
        log_events: Iterable[LogEvent],
        cache_key: str,
        ttl_seconds: TtlSeconds | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> dict[str, int]:
        """Write log events to cache.

        Args:
            log_events: Iterator of log events to cache.
            cache_key: Cache key to store under. Use generate_cache_key() to create.
            ttl_seconds: TTL for this entry in seconds; fractions are allowed. If None,
                uses default_ttl_seconds.
                Pass None explicitly to override default and use no expiration.
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

        with self._inflight_lock:
            self._inflight.add(parquet_path)
        try:
            # Write events to Parquet
            stats = write_log_events_to_parquet(
                log_events,
                parquet_path,
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
        finally:
            with self._inflight_lock:
                self._inflight.discard(parquet_path)

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
            >>> events = list(cache.read('cache:v2:abc123'))
            >>> for event in events:
            ...     print(event.message)
        """
        # Get Parquet path from metadata
        metadata_value = self._metadata.get(cache_key)

        if metadata_value is None:
            return iter(())

        parquet_path_str = _metadata_path(metadata_value)

        parquet_path = Path(parquet_path_str)

        # Check if file exists (may have been deleted externally)
        if not parquet_path.exists():
            # Clean up stale metadata
            self._metadata.delete(cache_key)
            return iter(())

        # Read and return events
        return read_parquet_to_log_events(parquet_path)

    def read_payload(self, cache_key: str) -> dict[str, Any] | None:
        """Read a JSON-serializable payload previously stored under a cache key.

        Args:
            cache_key: Cache key to read from.

        Returns:
            The stored mapping, or None when the key is missing, expired, or
            holds a value that is not a mapping (an older payload format).
        """
        value = self._metadata.get(cache_key)

        return value if isinstance(value, dict) else None

    def write_payload(
        self,
        cache_key: str,
        payload: dict[str, Any],
        ttl_seconds: TtlSeconds | None = None,
    ) -> None:
        """Store a JSON-serializable payload under a cache key.

        Args:
            cache_key: Cache key to store under.
            payload: Mapping of JSON-serializable values.
            ttl_seconds: TTL for this entry in seconds. If None, uses
                default_ttl_seconds.
        """
        ttl = ttl_seconds if ttl_seconds is not None else self._default_ttl
        self._metadata.set(cache_key, payload, expire=ttl)

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

        parquet_path_str = _metadata_path(metadata_value)

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
