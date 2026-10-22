"""Cache storage module for CloudWatch Logs events.

Provides efficient caching of log events using Parquet files with ZSTD compression.
Supports TTL expiration and last-read eviction policies via DiskCache metadata
management, and a sparse mode that answers a query without persisting.
"""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
import statistics
import tempfile
from collections import defaultdict
from collections.abc import Iterable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from operator import itemgetter
from pathlib import Path
from threading import Lock
from typing import Any, TypedDict

import msgspec
import polars as pl
from diskcache import Cache, JSONDisk

from tail_cw.aws.events import LogEvent
from tail_cw.cache import reads as _reads
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

_TOP_GROUPS = 5
"""How many log groups ``cache status`` names by bytes held."""

_READS_WINDOW = timedelta(days=7)
"""Reporting window for the hit and miss counts in ``cache status``."""

STAGING_DIRNAME = 'staging'
"""Directory inside the cache for in-flight NDJSON and sparse Parquet files.

Living inside the cache means staging bytes count against the same size limit
the persisted files do, and a full system temp directory cannot take a fetch
down with it.
"""

_STALE_STAGING = timedelta(hours=1)
"""A staging file older than this belongs to a dead command, so the next one sweeps it."""

_RECENTLY_READ = timedelta(hours=24)
"""Eviction protects entries read back inside this window, per the degrade ladder."""


class DiskBudgetExceededError(Exception):
    """A fetch cannot fit inside the disk budget at any rung of the ladder."""


# Precompiled regex for detecting ISO8601/RFC3339 timestamps at start of message
# Matches formats like: 2025-01-01T12:00:00Z, 2025-01-01T12:00:00.123456+00:00
def generate_cache_key(
    log_group: str,
    start_time: datetime,
    end_time: datetime,
    log_stream_names: list[str] | None = None,
    region_name: str | None = None,
    profile_name: str | None = None,
    filter_pattern: str | None = None,
) -> str:
    """Generate a deterministic cache key from CloudWatch query parameters.

    Hashes the canonical JSON of the query parameters with BLAKE2b, personalized
    for domain separation. The ``cache:v2`` prefix keeps v1 files (which carried
    ``event_id`` and a raw duplicate of every JSON message) from being read back
    under the current schema.

    A whole-window fetch (``filter_pattern=None``) serves every filter asked of a
    segment, evaluated locally by the query engine, and this is the key every segment is
    checked under first. ``filter_pattern`` only changes the key when it is given, so a
    historical fetch that sends one to CloudWatch gets its own key rather than colliding
    with, or widening, the whole-window entry.

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
        filter_pattern: Optional CloudWatch filter pattern sent with the fetch.

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

    if filter_pattern is not None:
        canonical['filter_pattern'] = filter_pattern

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

    Entries are ``[path, size, log_group]`` today. JSON serialization turns the
    stored tuple into a list, and older caches hold ``[path, size]`` or a bare
    path string, so all three shapes have to read back.
    """
    if isinstance(metadata_value, (tuple, list)):
        return str(metadata_value[0])
    return str(metadata_value)


def _metadata_size(metadata_value: Any) -> int:
    """Extract the stored Parquet size, or 0 for an entry that never carried it."""
    if isinstance(metadata_value, (tuple, list)) and len(metadata_value) > 1:
        return int(metadata_value[1])
    return 0


def _metadata_group(metadata_value: Any) -> str | None:
    """Extract the log group an entry was written for, or None when unrecorded."""
    if not isinstance(metadata_value, (tuple, list)):
        return None
    tail = metadata_value[2:]
    return str(tail[0]) if tail and tail[0] is not None else None


def _birth(path: Path) -> datetime:
    """When a file was stored, from birthtime where the filesystem records it."""
    stat = path.stat()
    return datetime.fromtimestamp(getattr(stat, 'st_birthtime', stat.st_ctime), tz=UTC)


_encode = json.JSONEncoder(separators=(',', ':')).encode
"""Encode one value as compact JSON for the payload-repair pass, which runs once per failed
write rather than once per event, so the stdlib encoder's cost there does not matter."""

_ndjson_encoder = msgspec.json.Encoder()
"""Bound once: builds the hot-path NDJSON line, which stdlib ``json`` cannot beat (below)."""

_ndjson_decoder = msgspec.json.Decoder()
"""Bound once: validates a message is a JSON object before its text is trusted as one."""

_PAYLOAD_ROOT = 'parsed'
"""Column the decoded payload lands in, and the first segment of every reported payload path."""


@dataclass(frozen=True)
class PayloadRepair:
    """Payload keys the write pass had to change to fit Parquet.

    Attributes:
        dropped: Paths that held nothing but an empty JSON object.
        widened: Paths retyped to text, each with the JSON types it was logged
            as, e.g. ``parsed.count (number, string)``.
    """

    dropped: tuple[str, ...] = ()
    widened: tuple[str, ...] = ()

    def notices(self) -> list[str]:
        """Render one line per change, for a caller that has somewhere to show them."""
        lines = []
        if self.widened:
            lines.append(f'Widened {len(self.widened)} payload key(s) to text: {", ".join(self.widened)}')
        if self.dropped:
            lines.append(f'Dropped {len(self.dropped)} empty payload key(s): {", ".join(self.dropped)}')
        return lines


class WriteStats(TypedDict):
    """What one Parquet write produced.

    Attributes:
        total_events: Events written.
        jsonl_events: Of those, the ones whose message decoded as a JSON object.
        file_size_bytes: Size of the written Parquet file.
        dropped_payload_keys: Payload paths dropped as empty.
        widened_payload_keys: Payload paths retyped to text, with their types.
    """

    total_events: int
    jsonl_events: int
    file_size_bytes: int
    dropped_payload_keys: list[str]
    widened_payload_keys: list[str]


def _parse_jsonl_message(message: str) -> dict[str, Any] | None:
    """Return the message decoded as a JSON object, or None when it is not one."""
    if not is_jsonl_message(message):
        return None
    try:
        parsed = _ndjson_decoder.decode(message)
    except msgspec.DecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _epoch_us(moment: datetime) -> int:
    """Microseconds since the epoch, as an int the staged NDJSON can hold without quoting.

    Written instead of ``isoformat()`` because a Parquet-bound timestamp column is cast
    from this on the Polars side (:func:`_normalized_columns`) either way, and an int
    column costs the sink nothing extra to infer, unlike a string one.
    """
    return round(moment.timestamp() * 1_000_000)


def _log_events_to_ndjson_file(
    log_events: Iterable[LogEvent],
    output_path: Path,
    progress_callback: ProgressCallback | None = None,
) -> tuple[int, int]:
    """Write LogEvent instances to a temporary NDJSON file.

    Each line is one event, built as a dict and handed to :data:`_ndjson_encoder`
    rather than assembled field by field: msgspec's own JSON writer beats hand-built
    string splicing even after accounting for re-encoding a payload that was already
    JSON text, which a stdlib-``json``-based splice was written to avoid. Measured
    against 1.5M synthetic prod-API-shaped events: 166k events/s for the previous
    stdlib-``json``-plus-splicing shape against 251k events/s for this one, with the
    same JSON-object validation kept on every message.

    A message that decodes as a JSON object is stored as ``parsed`` (however it was
    written, since msgspec is doing the encoding either way now); anything else is
    stored as ``message`` verbatim.

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

    with output_path.open('wb') as handle:
        for event in log_events:
            total_events += 1

            if progress_callback and total_events % 1000 == 0:
                progress_callback(total_events, TOTAL_UNKNOWN, 'Parsing JSONL...')

            parsed = _parse_jsonl_message(event.message)
            record: dict[str, Any] = {
                'log_group': event.log_group,
                'log_stream': event.log_stream,
                'timestamp': _epoch_us(event.timestamp),
                'ingestion_time': _epoch_us(event.ingestion_time) if event.ingestion_time else None,
            }
            if parsed is None:
                record['message'] = event.message
            else:
                jsonl_events += 1
                record['parsed'] = parsed
            handle.write(_ndjson_encoder.encode(record))
            handle.write(b'\n')

    return total_events, jsonl_events


def write_log_events_to_parquet(
    log_events: Iterable[LogEvent],
    output_path: Path,
    progress_callback: ProgressCallback | None = None,
    *,
    staging_dir: Path | None = None,
) -> WriteStats:
    """Convert LogEvent instances to a compressed Parquet file.

    Streams through a temporary NDJSON file so no more than one event is held in
    Python at a time. The written schema is ``log_group``, ``log_stream``,
    ``timestamp`` and ``ingestion_time`` as UTC datetimes, ``message``, and a
    ``parsed`` struct, sorted by timestamp. ``message`` is populated only for
    lines that are not JSON objects; for the rest ``parsed`` is the record and
    :func:`read_parquet_to_log_events` rebuilds the text from it.

    A payload shape Parquet cannot hold is repaired rather than refused, and the
    keys it changed are named on the result: see :func:`_repair_payload_types`.

    Args:
        log_events: Iterator of log events to store.
        output_path: Path where the Parquet file will be written.
        progress_callback: Optional callable invoked during conversion with
            ``(current, total, status_message)``.
        staging_dir: Directory for the intermediate NDJSON file. The cache passes
            its own staging directory so the file counts against the cache's
            budget; ``None`` uses the system temp directory.

    Returns:
        Counts of what was written, plus the payload keys the repair pass
        dropped or widened.

    Raises:
        ValueError: If there are no events to write, or a payload shape survives
            the repair pass.
        OSError: If the output file cannot be written.
    """
    temp_file = None
    try:
        if staging_dir is not None:
            staging_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', dir=staging_dir, delete=False) as f:
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

        repair = _sink_ndjson_to_parquet(temp_file, output_path, progress_callback=progress_callback)
        return {
            'total_events': total_events,
            'jsonl_events': jsonl_events,
            'file_size_bytes': output_path.stat().st_size,
            'dropped_payload_keys': list(repair.dropped),
            'widened_payload_keys': list(repair.widened),
        }

    finally:
        if temp_file is not None and temp_file.exists():
            temp_file.unlink()


def _scan_ndjson(source: Path) -> pl.LazyFrame:
    # The whole file is scanned to infer the schema. Sampling the first N rows is
    # unsound over arbitrary log payloads: a key that is null in the sample and a string
    # later panics the Parquet writer, an int-then-string key fails to parse, and a key
    # first appearing after the sample is silently dropped and becomes unqueryable.
    lazy = pl.scan_ndjson(str(source), infer_schema_length=None)
    # maintain_order keeps events that share a millisecond in the order CloudWatch
    # returned them, which is the only ordering information they carry.
    return _normalized_columns(lazy).sort('timestamp', maintain_order=True)


_PAYLOAD_SHAPE_ERRORS = (pl.exceptions.ComputeError, pl.exceptions.InvalidOperationError, pl.exceptions.SchemaError)
"""What a payload shape Parquet cannot hold raises as.

A key logged as two conflicting scalar types fails at ``sink_parquet`` with
``ComputeError``/``InvalidOperationError``; two records whose ``parsed`` structs
cannot be reconciled into one supertype fail earlier, while merely building the
scan's schema, as ``SchemaError``. Both are the repair pass's problem to fix.
"""


def _sink_ndjson_to_parquet(
    source: Path,
    output_path: Path,
    *,
    progress_callback: ProgressCallback | None,
) -> PayloadRepair:
    """Write the staged NDJSON as Parquet, repairing the payload once if it will not fit.

    The repair costs a second pass over the staged file, so it is only paid on
    the failure it exists to clear.
    """
    try:
        _scan_ndjson(source).sink_parquet(str(output_path), compression='zstd')
    except _PAYLOAD_SHAPE_ERRORS as err:
        first_error = err
    else:
        return PayloadRepair((), ())

    if progress_callback:
        progress_callback(0, TOTAL_UNKNOWN, 'Repairing payload types...')
    repair = _repair_payload_types(source)
    if not repair.dropped and not repair.widened:
        raise _unwritable_payload_error(_unnormalized_scan(source), first_error) from first_error
    try:
        _scan_ndjson(source).sink_parquet(str(output_path), compression='zstd')
    except _PAYLOAD_SHAPE_ERRORS as err:
        raise _unwritable_payload_error(_unnormalized_scan(source), err) from err
    return repair


def _unnormalized_scan(source: Path) -> pl.LazyFrame:
    """A raw scan for error reporting, when even ``_scan_ndjson`` could not build one.

    ``_unwritable_payload_error`` collects this frame's schema on a best-effort
    basis and already tolerates that failing too.
    """
    return pl.scan_ndjson(str(source), infer_schema_length=None)


def _normalized_columns(lazy: pl.LazyFrame) -> pl.LazyFrame:
    """Give the frame the full v2 schema with real datetime columns.

    Timestamps arrive as epoch-microsecond ints, and ``message`` or ``parsed``
    are absent entirely when every line in the batch went the other way, so
    both are materialized as typed nulls to keep one schema across every
    cached file. ``ingestion_time`` lands as ``Null`` rather than ``Int64``
    when every event in the batch omitted it, so the epoch cast only applies
    where there is an int column to cast.
    """
    schema = lazy.collect_schema()
    casts = [
        pl.from_epoch(pl.col(name), time_unit='us').dt.replace_time_zone('UTC')
        for name in ('timestamp', 'ingestion_time')
        if schema.get(name) == pl.Int64
    ]
    fills = [
        pl.lit(None, dtype=pl.String).alias(name)
        for name in ('message', 'ingestion_time')
        if name not in schema or schema[name] == pl.Null
    ]
    return lazy.with_columns(*casts, *fills)


def _json_kind(value: Any) -> str | None:
    """Name the Parquet-relevant shape of one JSON value, or None for a null.

    A null never conflicts, because Polars widens it to whatever the other rows
    hold. ``int`` and ``float`` share one name for the same reason.
    """
    match value:
        case None:
            return None
        case bool():
            return 'bool'
        case int() | float():
            return 'number'
        case dict():
            return 'object'
        case list():
            return 'list'
        case _:
            return 'string'


def _observe_payload(value: Any, path: str, kinds: dict[str, set[str]], populated: set[str]) -> None:
    kind = _json_kind(value)
    if kind is None:
        return
    kinds.setdefault(path, set()).add(kind)
    if kind == 'object':
        if value:
            populated.add(path)
        for key, item in value.items():
            _observe_payload(item, f'{path}.{key}', kinds, populated)
    elif kind == 'list':
        for item in value:
            _observe_payload(item, f'{path}[]', kinds, populated)


def _observe_ndjson_payloads(source: Path) -> tuple[dict[str, set[str]], set[str]]:
    kinds: dict[str, set[str]] = {}
    populated: set[str] = set()
    with source.open(encoding='utf-8') as handle:
        for line in handle:
            payload = json.loads(line).get('parsed')
            if payload is not None:
                _observe_payload(payload, _PAYLOAD_ROOT, kinds, populated)
    return kinds, populated


def _without_covered_descendants(paths: set[str], covered: set[str]) -> set[str]:
    """Drop any path an entry of ``covered`` already stands in for.

    Widening a key to text takes its whole subtree with it, so naming a child as
    well would report a key the repair never looked at.
    """
    return {path for path in paths if not any(path.startswith(f'{parent}.') for parent in covered)}


def _scalar_text(value: Any) -> Any:
    if value is None or isinstance(value, str):
        return value
    return _encode(value)


def _coerce_payload(value: Any, path: str, widen: set[str], drop: set[str]) -> Any:
    if path in widen:
        return _encode(value) if isinstance(value, (dict, list)) else _scalar_text(value)
    if isinstance(value, dict):
        coerced = {
            key: _coerce_payload(item, f'{path}.{key}', widen, drop)
            for key, item in value.items()
            if f'{path}.{key}' not in drop
        }
        # A key whose object was emptied here, or was empty to begin with, infers a
        # zero-field struct in its parent exactly as the original key did.
        return {key: item for key, item in coerced.items() if item != {}}
    if isinstance(value, list):
        return [_coerce_payload(item, f'{path}[]', widen, drop) for item in value]
    return value


def _rewrite_ndjson_payloads(source: Path, widen: set[str], drop: set[str]) -> None:
    """Rewrite ``source`` in place with the named payload paths widened and dropped."""
    staged = source.with_suffix('.repaired')
    with source.open(encoding='utf-8') as reader, staged.open('w', encoding='utf-8') as writer:
        for line in reader:
            record = json.loads(line)
            payload = record.get('parsed')
            if payload is None:
                writer.write(line)
                continue
            coerced = _coerce_payload(payload, _PAYLOAD_ROOT, widen, drop)
            if coerced == {}:
                # Nothing is left to store as a struct, and the text is still the record.
                del record['parsed']
                record['message'] = _encode(payload)
            else:
                record['parsed'] = coerced
            writer.write(_encode(record) + '\n')
    staged.replace(source)


def _repair_payload_types(source: Path) -> PayloadRepair:
    """Make every payload in the staged NDJSON representable as one Parquet struct.

    Two shapes defeat Polars' inference. A key that is always an empty JSON
    object infers a zero-field struct Parquet cannot hold, and carries nothing,
    so it is dropped. A key logged as two different JSON types cannot be parsed
    into either, so its whole subtree is widened to text, which stays searchable
    and stays visible in the record detail.

    Widening a key retypes it silently unless the caller says which keys moved,
    which is why both sets come back rather than being applied and forgotten.
    """
    kinds, populated = _observe_ndjson_payloads(source)
    widen = {path for path, seen in kinds.items() if len(seen) > 1}
    widen = _without_covered_descendants(widen, widen)
    drop = _without_covered_descendants(
        {path for path, seen in kinds.items() if seen == {'object'} and path not in populated},
        widen,
    )
    if not widen and not drop:
        return PayloadRepair((), ())
    _rewrite_ndjson_payloads(source, widen, drop)
    return PayloadRepair(
        dropped=tuple(sorted(drop)),
        widened=tuple(f'{path} ({", ".join(sorted(kinds[path]))})' for path in sorted(widen)),
    )


def _empty_struct_paths(dtype: Any, path: str) -> list[str]:
    if isinstance(dtype, pl.Struct):
        if not dtype.fields:
            return [path]
        return [found for field in dtype.fields for found in _empty_struct_paths(field.dtype, f'{path}.{field.name}')]
    if isinstance(dtype, pl.List):
        return _empty_struct_paths(dtype.inner, f'{path}[]')
    return []


def _unwritable_payload_error(frame: pl.LazyFrame, err: Exception) -> ValueError:
    """Name the payload key behind a Polars dtype failure.

    Polars reports the dtype it could not handle and never the key that produced
    it, so the bare message cannot be acted on. An empty JSON object infers a
    zero-field struct Parquet cannot represent; one key logged as two scalar
    types fails to parse into the type inference picked.
    """
    try:
        schema = frame.collect_schema()
    except Exception:
        return ValueError(str(err))
    empties = [found for name, dtype in schema.items() for found in _empty_struct_paths(dtype, name)]
    if empties:
        return ValueError(f'{err}. Empty JSON object at {", ".join(sorted(empties))}, which carries no value to store')
    return ValueError(f'{err}. One payload key is logged as more than one scalar type')


def parquet_row_count(parquet_path: Path) -> int:
    """Count the rows in a cached Parquet file, from its footer rather than its data."""
    return int(pl.scan_parquet(str(parquet_path)).select(pl.len()).collect().item())


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
        top_groups: The log groups holding the most cached bytes, largest first.
            Entries written before the group was recorded count under None.
        reads: Hit and miss accounting over the reporting window, from the
            append-only read log. None when nothing has been recorded yet.
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
    top_groups: tuple[tuple[str | None, int], ...] = ()
    reads: _reads.ReadStats | None = None

    @property
    def fraction_used(self) -> float:
        """Share of the limit in use, 0.0 when there is no limit to speak of."""
        return self.bytes_used / self.bytes_limit if self.bytes_limit > 0 else 0.0


@dataclass(frozen=True)
class _LiveEntry:
    """A ``cache:v2`` key whose Parquet file is on disk.

    Attributes:
        path: The Parquet file.
        size: Its stored byte count.
        group: The log group recorded at write time, when it was.
        expires_at: The write-time expiry for a transient entry, or None for a
            settled one (settled entries age out by last read, not write).
    """

    path: Path
    size: int
    group: str | None
    expires_at: datetime | None


@dataclass(frozen=True)
class _EntryScan:
    """One pass over the metadata store, shared by ``status`` and the sweeps.

    Attributes:
        entries: Metadata rows seen, whatever they point at.
        referenced: Parquet paths any entry names.
        stale: Entries whose file is gone.
        live: ``cache:v2`` keys with a file on disk.
        path_to_keys: Index from a live Parquet path back to its key, so an
            eviction never walks every key to find one file's owner.
        group_bytes: Log group to the bytes it holds, for entries that carry one.
    """

    entries: int
    referenced: set[Path]
    stale: int
    live: dict[str, _LiveEntry]
    path_to_keys: dict[Path, str]
    group_bytes: dict[str | None, int]


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
        *,
        min_free_mb: int = 0,
        max_group_mb: int = 0,
        max_segment_mb: int = 0,
        sparse_only: bool = False,
    ) -> None:
        """Initialize LogCache with specified configuration.

        Args:
            cache_dir: Directory for cache storage. Created if it doesn't exist.
            size_limit_mb: Maximum cache size in MB. Default 1000 (1GB).
            default_ttl_seconds: TTL for settled cache entries, counted from the
                entry's last read rather than its write, so a segment read every
                day does not expire under itself. None means no expiration.
            eviction_policy: DiskCache eviction policy. Default 'least-recently-stored'
                for FIFO behavior. See DiskCache docs for other options.
            min_free_mb: Stop persisting when free space on the cache's volume
                drops below this many MB; the command is answered in sparse mode
                instead. 0 disables the floor.
            max_group_mb: Largest share one log group may hold; a group past its
                quota loses its least-recently-read segments first. 0 disables
                the quota.
            max_segment_mb: Largest segment size that may persist; a bigger
                fetch is answered sparse rather than stored. 0 disables the cap.
            sparse_only: Fetch and answer without persisting anything, as if
                every write had already stepped down the degrade ladder.

        Raises:
            OSError: If cache directory cannot be created.
        """
        self._cache_dir = cache_dir
        self._parquet_dir = cache_dir / 'parquet'
        self._read_ttl = default_ttl_seconds
        self._size_limit_bytes = size_limit_mb * 1024 * 1024
        self._min_free_bytes = min_free_mb * 1024 * 1024
        self._max_group_bytes = max_group_mb * 1024 * 1024
        self._max_segment_bytes = max_segment_mb * 1024 * 1024
        self._sparse_only = sparse_only
        self.sparse: list[Path] = []
        self._sparse_sizes: dict[Path, int] = {}
        self._inflight: set[Path] = set()
        self._inflight_lock = Lock()
        # Bookkeeping kept current by every mutation below, so the per-segment
        # ladder decision never rescans the directory or the metadata.
        self._written: set[Path] = set()
        self._sizes_seeded = False
        self._all_sizes: list[int] = []
        self._group_sizes: dict[str | None, list[int]] = defaultdict(list)
        self._command_depth = 0
        self._last_reads_marker: tuple[int, int] | None = None
        self._last_reads: dict[str, datetime] = {}

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
        # Lazily seeded from disk on first budget question, then adjusted by
        # every write and delete; a file removed outside this object leaves it
        # high, which errs toward sparse mode rather than overflow.
        self._bytes_on_disk: int | None = None

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

    def _track_entry(self, path: Path, size: int, group: str | None) -> None:
        """Account a new live entry in the incremental totals."""
        self._written.add(path)
        if self._bytes_on_disk is not None:
            self._bytes_on_disk += size
        if self._sizes_seeded:
            self._all_sizes.append(size)
            self._group_sizes[group].append(size)

    def _untrack_size(self, size: int, group: str | None) -> None:
        """Drop one entry's size from the estimate index, when it was seeded."""
        if not self._sizes_seeded:
            return
        with suppress(ValueError):
            self._all_sizes.remove(size)
        with suppress(ValueError):
            self._group_sizes[group].remove(size)

    def _drop_entry(self, cache_key: str, entry: _LiveEntry, scan: _EntryScan | None = None) -> None:
        """Delete one entry everywhere at once: metadata, file, totals, scan.

        Every eviction path goes through here so the incremental counters can
        never drift from what is actually on disk.
        """
        self._metadata.delete(cache_key)
        entry.path.unlink(missing_ok=True)
        if self._bytes_on_disk is not None:
            self._bytes_on_disk -= entry.size
        self._untrack_size(entry.size, entry.group)
        self._written.discard(entry.path)
        if scan is not None:
            scan.live.pop(cache_key, None)
            scan.path_to_keys.pop(entry.path, None)
            scan.referenced.discard(entry.path)
            if entry.group in scan.group_bytes:
                scan.group_bytes[entry.group] -= entry.size

    def _seed_sizes(self) -> None:
        """Build the estimate index once per cache instance."""
        if self._sizes_seeded:
            return
        scan = self._scan_entries()
        for entry in scan.live.values():
            self._all_sizes.append(entry.size)
            self._group_sizes[entry.group].append(entry.size)
        self._sizes_seeded = True

    def _cleanup_orphaned_files(self, scan: _EntryScan) -> int:
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
            cleaned_count = 0
            for parquet_file in self._parquet_dir.glob('*.parquet'):
                if parquet_file in scan.referenced or parquet_file in inflight:
                    continue
                try:
                    size = parquet_file.stat().st_size
                except OSError:
                    continue
                parquet_file.unlink(missing_ok=True)
                if self._bytes_on_disk is not None:
                    self._bytes_on_disk -= size
                cleaned_count += 1

        return cleaned_count

    def _enforce_parquet_size_limit(
        self,
        scan: _EntryScan | None = None,
        last_read: dict[str, datetime] | None = None,
    ) -> int:
        """Enforce the size limit by evicting least-recently-read settled entries.

        Staging files count against the limit too, since they live inside the
        cache; only persisted entries are evicted to make room.

        Returns:
            Number of entries evicted.
        """
        return self._evict_for(0, scan=scan, last_read=last_read)

    def _evict_for(
        self,
        needed_bytes: int,
        *,
        now: datetime | None = None,
        scan: _EntryScan | None = None,
        last_read: dict[str, datetime] | None = None,
    ) -> int:
        """Evict settled entries least-recently-read first until ``needed_bytes`` fits.

        Anything read inside ``_RECENTLY_READ``, anything with its own TTL, and
        anything inflight or written by this command is skipped, per the degrade
        ladder. ``last_read`` is deliberately re-parsed per call when not given:
        a hit served seconds ago is exactly what the 24-hour protection checks.

        Returns:
            Number of entries evicted.
        """
        moment = now or datetime.now(UTC)
        used = self._size_on_disk()
        if used + needed_bytes <= self._size_limit_bytes:
            return 0
        scan = scan or self._scan_entries()
        if last_read is None:
            last_read = self._fresh_last_reads()
        candidates: list[tuple[datetime, str, _LiveEntry]] = []
        for key, entry in scan.live.items():
            if entry.expires_at is not None or entry.path in self._written:
                continue
            read = last_read.get(key)
            if read is not None and moment - read < _RECENTLY_READ:
                continue
            try:
                candidates.append((read or _birth(entry.path), key, entry))
            except OSError:
                continue
        candidates.sort(key=itemgetter(0))

        # Held for the whole pass for the same reason as the orphan sweep: a
        # concurrent writer must not have its file evicted between the scan and
        # the delete.
        evicted = 0
        with self._inflight_lock:
            inflight = set(self._inflight)
            for _activity, key, entry in candidates:
                if used + needed_bytes <= self._size_limit_bytes:
                    break
                if entry.path in inflight:
                    continue
                self._drop_entry(key, entry, scan)
                used -= entry.size
                evicted += 1
        return evicted

    def prepare_write(self, log_group: str | None, *, max_added: int | None, added: int) -> tuple[bool, str | None]:
        """Decide persist vs sparse for one segment, stepping down the ladder.

        The ladder is: fit, then evict the least-recently-read, then answer from
        a temp file, then refuse. ``added`` is how many bytes this command has
        written so far against ``max_added`` (``--max-disk``); both are ignored
        when ``max_added`` is None.

        Returns:
            ``(persist, notice)``: persist False means write sparse instead, and
            the notice names the ladder step that ran.

        Raises:
            DiskBudgetExceededError: When not even a sparse file fits the budget.
        """
        if self._sparse_only:
            return False, None
        estimate = self._estimate_segment_bytes(log_group)
        if self._max_segment_bytes and estimate > self._max_segment_bytes:
            return False, 'cache: sparse (segment exceeds the max_segment_mb cap), not persisting'
        if max_added is not None and added + estimate > max_added:
            if estimate > max_added:
                msg = (
                    f'A ~{estimate / 1_048_576:.0f} MB segment cannot fit --max-disk '
                    f'{max_added / 1_048_576:.0f} MB; raise --max-disk or narrow the window'
                )
                raise DiskBudgetExceededError(msg)
            return False, 'cache: sparse (--max-disk reached), not persisting'
        free = self._free_bytes()
        if free - estimate <= self._min_free_bytes:
            if free - estimate <= self._min_free_bytes // 10:
                msg = (
                    f'A ~{estimate / 1_048_576:.0f} MB segment cannot fit {free / 1_073_741_824:.1f} GB '
                    'free without exhausting the volume; free space or narrow the window'
                )
                raise DiskBudgetExceededError(msg)
            return False, (
                f'cache: sparse (free disk {free / 1_073_741_824:.1f} GB is under the '
                f'{self._min_free_bytes / 1_073_741_824:g} GB floor), not persisting'
            )
        if estimate and self._would_exceed_limit(estimate):
            self._evict_for(estimate)
            if self._would_exceed_limit(estimate):
                return False, 'cache: sparse (cap held by recently read entries), not persisting'
        return True, None

    def _would_exceed_limit(self, incoming_bytes: int) -> bool:
        return self._size_on_disk() + incoming_bytes > self._size_limit_bytes

    def _free_bytes(self) -> int:
        return shutil.disk_usage(self._cache_dir).free

    @property
    def sparse_only(self) -> bool:
        """True when this cache was opened to fetch and answer without persisting."""
        return self._sparse_only

    def _size_on_disk(self) -> int:
        if self._bytes_on_disk is None:
            self._bytes_on_disk = self._glob_disk_bytes()
        return self._bytes_on_disk

    def _glob_disk_bytes(self) -> int:
        total = 0
        for directory in (self._parquet_dir, self._cache_dir / STAGING_DIRNAME):
            if directory.is_dir():
                total += sum(child.stat().st_size for child in directory.iterdir() if child.is_file())
        return total

    def _staging_dir(self) -> Path:
        path = self._cache_dir / STAGING_DIRNAME
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _estimate_segment_bytes(self, log_group: str | None) -> int:
        """Predict a segment's Parquet size from what the group has written before.

        Falls back to the median across every group, then to 0 when there is no
        history at all; a missed guess is caught by the post-write enforce pass,
        so underestimating only means the ladder steps down one segment later.
        The size index is built once per cache instance and kept current by the
        writes and evictions that follow, so this never rescans the metadata.
        """
        self._seed_sizes()
        pool = self._group_sizes.get(log_group) or self._all_sizes
        return int(statistics.median(pool)) if pool else 0

    def write_sparse(self, log_events: Iterable[LogEvent], *, log_group: str | None = None) -> Path:
        """Write events to a Parquet file inside staging, persisting nothing.

        The file answers the current command and is deleted at its end; it gets
        no metadata entry, so nothing points a later read at it. Its size still
        counts against the size limit while it lives, and it is recorded as
        fetched bytes like a persisted write.
        """
        with tempfile.NamedTemporaryFile(suffix='.parquet', dir=self._staging_dir(), delete=False) as handle:
            path = Path(handle.name)
        stats = write_log_events_to_parquet(log_events, path, staging_dir=self._staging_dir())
        self.sparse.append(path)
        self._sparse_sizes[path] = stats['file_size_bytes']
        if self._bytes_on_disk is not None:
            self._bytes_on_disk += stats['file_size_bytes']
        _reads.record(
            self._cache_dir,
            op='write',
            key=f'sparse:{path.name}',
            log_group=log_group,
            bytes_served=stats['file_size_bytes'],
        )
        return path

    def _fresh_last_reads(self) -> dict[str, datetime]:
        """Parse the read log again only when it has grown on disk.

        The log is append-only, so ``(mtime, size)`` changes exactly when the
        content does — the 24-hour protection still sees a hit served seconds
        ago.
        """
        try:
            stat = (self._cache_dir / _reads.READS_FILENAME).stat()
            marker = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            marker = None
        if self._last_reads_marker != marker:
            self._last_reads = _reads.last_reads(self._cache_dir)
            self._last_reads_marker = marker
        return self._last_reads

    def maintain(self, *, now: datetime | None = None) -> int:
        """Run the once-per-command sweeps: expiry, orphans, read TTL, stale staging.

        Performed per command rather than per segment write, which is what keeps
        the file-system calls bounded on a wide fetch. Every pass shares one
        metadata scan and one read-log parse; evictions keep both current as
        they delete.

        Returns:
            Number of orphaned Parquet files cleaned up.
        """
        moment = now or datetime.now(UTC)
        self._metadata.expire()
        self._metadata.cull()
        scan = self._scan_entries()
        cleaned = self._cleanup_orphaned_files(scan)
        last_read = self._fresh_last_reads()
        self._expire_read_ttl(moment, scan=scan, last_read=last_read)
        self._enforce_parquet_size_limit(scan, last_read)
        self._enforce_group_quota(scan, last_read)
        self._sweep_staging(moment)
        _reads.compact(self._cache_dir)
        return cleaned

    def _enforce_group_quota(
        self,
        scan: _EntryScan | None = None,
        last_read: dict[str, datetime] | None = None,
    ) -> int:
        """Evict each group's least-recently-read entries until it fits its quota."""
        if not self._max_group_bytes:
            return 0
        scan = scan or self._scan_entries()
        if last_read is None:
            last_read = self._fresh_last_reads()
        candidates = [
            (last_read.get(key) or _birth(entry.path), key, entry.group, entry)
            for key, entry in scan.live.items()
            if entry.expires_at is None and entry.group is not None and entry.path not in self._written
        ]
        held: dict[str, int] = {}
        for _activity, _key, group, entry in candidates:
            held[group] = held.get(group, 0) + entry.size
        over = {group for group, total in held.items() if total > self._max_group_bytes}
        evicted = 0
        for _activity, key, group, entry in sorted(candidates, key=itemgetter(0)):
            if group not in over or held[group] <= self._max_group_bytes:
                continue
            self._drop_entry(key, entry, scan)
            held[group] -= entry.size
            evicted += 1
        return evicted

    def _expire_read_ttl(
        self,
        now: datetime,
        *,
        scan: _EntryScan | None = None,
        last_read: dict[str, datetime] | None = None,
    ) -> int:
        """Evict settled entries last read longer ago than the configured TTL."""
        if self._read_ttl is None:
            return 0
        cutoff = now - timedelta(seconds=float(self._read_ttl))
        scan = scan or self._scan_entries()
        if last_read is None:
            last_read = self._fresh_last_reads()
        evicted = 0
        for key, entry in list(scan.live.items()):
            if entry.expires_at is not None:
                continue
            try:
                activity = last_read.get(key) or _birth(entry.path)
            except OSError:
                continue
            if activity < cutoff:
                self._drop_entry(key, entry, scan)
                evicted += 1
        return evicted

    def _sweep_staging(self, now: datetime) -> int:
        """Delete staging files old enough that their writer must be gone."""
        staging = self._cache_dir / STAGING_DIRNAME
        if not staging.is_dir():
            return 0
        swept = 0
        for child in staging.iterdir():
            if not child.is_file() or child in self.sparse:
                continue
            try:
                stat = child.stat()
            except OSError:
                continue
            if now.timestamp() - stat.st_mtime > _STALE_STAGING.total_seconds():
                child.unlink(missing_ok=True)
                if self._bytes_on_disk is not None:
                    self._bytes_on_disk -= stat.st_size
                swept += 1
        return swept

    def drop_sparse(self) -> None:
        """Delete every sparse file this cache wrote, when the command is done with them."""
        for path in self.sparse:
            if self._bytes_on_disk is not None:
                self._bytes_on_disk -= self._sparse_sizes.get(path, 0)
            path.unlink(missing_ok=True)
        self.sparse.clear()
        self._sparse_sizes.clear()

    def status(self) -> CacheStatus:
        """Report what is on disk against the limit, without changing anything.

        Counts files rather than trusting the metadata, because eviction deletes a file
        and its entry separately and a crash between the two leaves one of each behind.
        """
        scan = self._scan_entries()

        files = 0
        used = 0
        stamps: list[float] = []
        orphans = 0
        for parquet_file in self._parquet_dir.glob('*.parquet'):
            stat = parquet_file.stat()
            files += 1
            used += stat.st_size
            stamps.append(getattr(stat, 'st_birthtime', stat.st_ctime))
            if parquet_file not in scan.referenced:
                orphans += 1

        top_groups = tuple(sorted(scan.group_bytes.items(), key=itemgetter(1), reverse=True)[:_TOP_GROUPS])
        read_stats = None
        if (self._cache_dir / _reads.READS_FILENAME).exists():
            read_stats = _reads.summarize(
                self._cache_dir,
                {key: entry.size for key, entry in scan.live.items()},
                since=datetime.now(UTC) - _READS_WINDOW,
            )

        return CacheStatus(
            cache_dir=self._cache_dir,
            files=files,
            bytes_used=used,
            bytes_limit=self._size_limit_bytes,
            oldest=datetime.fromtimestamp(min(stamps), tz=UTC) if stamps else None,
            newest=datetime.fromtimestamp(max(stamps), tz=UTC) if stamps else None,
            entries=scan.entries,
            stale_entries=scan.stale,
            orphan_files=orphans,
            default_ttl_seconds=self._read_ttl,
            top_groups=top_groups,
            reads=read_stats,
        )

    def _scan_entries(self) -> _EntryScan:
        """Walk the metadata once, returning everything a sweep or report needs."""
        referenced: set[Path] = set()
        path_to_keys: dict[Path, str] = {}
        stale = 0
        entries = 0
        live: dict[str, _LiveEntry] = {}
        group_bytes: dict[str | None, int] = {}
        for cache_key in list(self._metadata.iterkeys()):  # type: ignore[attr-defined]
            metadata_value, expire_time = self._metadata.get(cache_key, expire_time=True)  # type: ignore[call-overload]
            if metadata_value is None:
                continue
            entries += 1
            path = Path(_metadata_path(metadata_value))
            referenced.add(path)
            if not path.exists():
                stale += 1
                continue
            if not (isinstance(cache_key, str) and cache_key.startswith(f'{CACHE_KEY_PREFIX}:')):
                continue
            size = _metadata_size(metadata_value)
            group = _metadata_group(metadata_value)
            live[cache_key] = _LiveEntry(
                path=path,
                size=size,
                group=group,
                expires_at=(datetime.fromtimestamp(float(expire_time), UTC) if expire_time else None),
            )
            path_to_keys[path] = cache_key
            group_bytes[group] = group_bytes.get(group, 0) + size
        return _EntryScan(
            entries=entries,
            referenced=referenced,
            stale=stale,
            live=live,
            path_to_keys=path_to_keys,
            group_bytes=group_bytes,
        )

    def write(
        self,
        log_events: Iterable[LogEvent],
        cache_key: str,
        ttl_seconds: TtlSeconds | None = None,
        progress_callback: ProgressCallback | None = None,
        *,
        log_group: str | None = None,
    ) -> WriteStats:
        """Write log events to cache.

        Args:
            log_events: Iterator of log events to cache.
            cache_key: Cache key to store under. Use generate_cache_key() to create.
            ttl_seconds: Write-time TTL for this entry in seconds; fractions are
                allowed. Settled segments pass None and are reclaimed by the
                configured read TTL instead, which counts from the last read.
            progress_callback: Optional callable notified of progress updates.
            log_group: The group the events came from, stored with the entry so a
                key that is only a hash still has a group to report against.

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
                staging_dir=self._staging_dir(),
            )

            # Store metadata in DiskCache with file size for efficient size tracking.
            # Only an explicit TTL becomes a write-time expire: the configured TTL
            # counts from the last read instead, applied by _expire_read_ttl.
            metadata_value = (str(parquet_path), stats['file_size_bytes'], log_group)
            self._metadata.set(
                cache_key,
                metadata_value,
                expire=ttl_seconds,
            )
            self._track_entry(parquet_path, stats['file_size_bytes'], log_group)
            _reads.record(
                self._cache_dir,
                op='write',
                key=cache_key,
                log_group=log_group,
                bytes_served=stats['file_size_bytes'],
            )
        finally:
            with self._inflight_lock:
                self._inflight.discard(parquet_path)

        # Sweeps run once per command in maintain(), not per write: a wide fetch
        # would otherwise pay a full metadata scan and orphan walk per segment.
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
        ttl = ttl_seconds if ttl_seconds is not None else self._read_ttl
        self._metadata.set(cache_key, payload, expire=ttl)

    def exists(self, cache_key: str) -> bool:
        """Check if a cache key exists.

        Args:
            cache_key: Cache key to check.

        Returns:
            True if key exists and is valid, False otherwise.
        """
        return self.get_parquet_path(cache_key, record=False) is not None

    def get_parquet_path(
        self,
        cache_key: str,
        *,
        log_group: str | None = None,
        record: bool = True,
    ) -> Path | None:
        """Return the Parquet file path for a cache key.

        Args:
            cache_key: Cache key to look up.
            log_group: The group being read, for the read log's per-group
                accounting. Unneeded when the entry is already written, since the
                hit then reports the group the metadata carries.
            record: Whether the lookup counts as a read. False for the
                post-write path lookup, which would otherwise mark every write
                as immediately re-read.

        Returns:
            Path to the cached Parquet file, or None when the key is missing,
            expired, or the file was deleted externally (stale metadata is
            cleaned up).
        """
        metadata_value = self._metadata.get(cache_key)

        if metadata_value is None:
            if record:
                _reads.record(self._cache_dir, op='miss', key=cache_key, log_group=log_group)
            return None

        parquet_path_str = _metadata_path(metadata_value)

        parquet_path = Path(parquet_path_str)
        if not parquet_path.exists():
            # Clean up stale metadata
            self._metadata.delete(cache_key)
            if record:
                _reads.record(self._cache_dir, op='miss', key=cache_key, log_group=log_group)
            return None

        if record:
            _reads.record(
                self._cache_dir,
                op='hit',
                key=cache_key,
                log_group=_metadata_group(metadata_value) or log_group,
                bytes_served=_metadata_size(metadata_value),
            )
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
        return self.maintain()

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
        self._bytes_on_disk = self._glob_disk_bytes() if self._bytes_on_disk is not None else None
        self._written.clear()
        self._sparse_sizes.clear()
        self._sizes_seeded = False
        self._all_sizes.clear()
        self._group_sizes.clear()

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

    @contextmanager
    def command_scope(self) -> Iterator[LogCache]:
        """Reset per-command state on a long-lived cache for one command's lifetime.

        Resets only at the start of the first of possibly overlapping commands,
        so one command never strips another's ``_written``/``sparse`` shield.
        Sparse files the caller already dropped get uncounted here — the only
        place a long-lived cache learns they are gone.

        Yields:
            This cache, for ``with`` chaining.
        """
        self._command_depth += 1
        if self._command_depth == 1:
            self._written.clear()
            for path in list(self.sparse):
                if not path.exists():
                    self.sparse.remove(path)
                    if self._bytes_on_disk is not None:
                        self._bytes_on_disk -= self._sparse_sizes.pop(path, 0)
        try:
            yield self
        finally:
            self._command_depth -= 1
