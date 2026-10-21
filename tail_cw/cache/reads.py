"""An append-only record of what the cache serves.

What is re-read decides the eviction and TTL policies, and nothing else can answer
it: a cache key is a hash of the query, and DiskCache's ``access_count`` needs a
metadata write on the read path, which is the contention this record exists to
avoid. One JSON line per lookup and per write, appended after the call it
describes; a lost line costs a measurement, never an answer.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

READS_FILENAME = 'reads.ndjson'

_KEPT = timedelta(days=31)
"""How far back the log reaches once compacted; eviction only needs recent reads."""

_COMPACT_ABOVE_BYTES = 4 * 1024 * 1024
"""The log is rewritten to its kept window when it grows past this."""

ReadOp = Literal['hit', 'miss', 'write']


def record(
    cache_dir: Path,
    *,
    op: ReadOp,
    key: str,
    log_group: str | None = None,
    bytes_served: int = 0,
    now: datetime | None = None,
) -> None:
    """Append one line to the read log.

    Instrumentation never raises: a failed accounting write must not fail the
    command it decorates.
    """
    entry: dict[str, Any] = {'ts': (now or datetime.now(UTC)).isoformat(), 'op': op, 'key': key}
    if log_group is not None:
        entry['group'] = log_group
    if bytes_served:
        entry['bytes'] = bytes_served
    try:
        with (cache_dir / READS_FILENAME).open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(entry, separators=(',', ':')) + '\n')
    except OSError:
        return


@dataclass(frozen=True)
class ReadRecord:
    """One parsed line of the read log."""

    ts: datetime
    op: str
    key: str
    log_group: str | None
    bytes_served: int


def iter_records(cache_dir: Path) -> Iterator[ReadRecord]:
    """Yield each parseable record; a truncated or foreign line is skipped."""
    path = cache_dir / READS_FILENAME
    try:
        lines = path.read_text(encoding='utf-8').splitlines()
    except OSError:
        return
    for line in lines:
        record_ = _parse_record(line)
        if record_ is not None:
            yield record_


def _parse_record(line: str) -> ReadRecord | None:
    try:
        entry = json.loads(line)
        ts = datetime.fromisoformat(entry['ts'])
    except (ValueError, KeyError, TypeError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    group = entry.get('group')
    return ReadRecord(
        ts=ts,
        op=str(entry.get('op')),
        key=str(entry.get('key')),
        log_group=str(group) if group is not None else None,
        bytes_served=int(entry.get('bytes') or 0),
    )


def last_reads(cache_dir: Path) -> dict[str, datetime]:
    """The most recent hit per cache key, for last-read eviction ordering."""
    latest: dict[str, datetime] = {}
    for entry in iter_records(cache_dir):
        if entry.op == 'hit':
            latest[entry.key] = max(latest.get(entry.key, datetime.min.replace(tzinfo=UTC)), entry.ts)
    return latest


@dataclass(frozen=True)
class ReadStats:
    """Read activity over the reporting window, for ``cache status``.

    Attributes:
        hits: Lookups the cache served in the window.
        misses: Lookups it did not.
        hit_rate: hits / (hits + misses), or None when nothing was looked up.
        bytes_served: Parquet bytes read back on hits in the window.
        bytes_fetched: Parquet bytes written by fetches in the window.
        bytes_never_read: Live bytes whose key has no hit on record.
        oldest_read: The least recent last-read among live entries, or None when
            no live entry has ever been read back.
    """

    hits: int
    misses: int
    hit_rate: float | None
    bytes_served: int
    bytes_fetched: int
    bytes_never_read: int
    oldest_read: datetime | None


def summarize(
    cache_dir: Path,
    live_entries: Mapping[str, int],
    *,
    since: datetime,
) -> ReadStats:
    """Aggregate the log against the entries the cache currently holds.

    Args:
        cache_dir: Cache root holding ``reads.ndjson``.
        live_entries: Cache key to its Parquet size, for entries on disk now.
        since: Start of the reporting window for hit/miss counts.
    """
    hits = misses = bytes_served = bytes_fetched = 0
    last_read: dict[str, datetime] = {}
    for entry in iter_records(cache_dir):
        if entry.op == 'hit':
            last_read[entry.key] = max(last_read.get(entry.key, datetime.min.replace(tzinfo=UTC)), entry.ts)
            if entry.ts >= since:
                hits += 1
                bytes_served += entry.bytes_served
        elif entry.op == 'miss':
            misses += entry.ts >= since
        elif entry.op == 'write' and entry.ts >= since:
            bytes_fetched += entry.bytes_served
    never_read = sum(size for key, size in live_entries.items() if key not in last_read)
    live_reads = [last_read[key] for key in live_entries if key in last_read]
    return ReadStats(
        hits=hits,
        misses=misses,
        hit_rate=hits / (hits + misses) if hits + misses else None,
        bytes_served=bytes_served,
        bytes_fetched=bytes_fetched,
        bytes_never_read=never_read,
        oldest_read=min(live_reads) if live_reads else None,
    )


def compact(cache_dir: Path, *, now: datetime | None = None) -> int:
    """Rewrite the log to its kept window when it has grown past the cap.

    Returns:
        Number of records dropped, or 0 when the log needed no compaction.
    """
    moment = now or datetime.now(UTC)
    path = cache_dir / READS_FILENAME
    try:
        if path.stat().st_size <= _COMPACT_ABOVE_BYTES:
            return 0
    except OSError:
        return 0
    cutoff = moment - _KEPT
    kept = [
        json.dumps(
            {
                'ts': entry.ts.isoformat(),
                'op': entry.op,
                'key': entry.key,
                **({'group': entry.log_group} if entry.log_group else {}),
                **({'bytes': entry.bytes_served} if entry.bytes_served else {}),
            },
            separators=(',', ':'),
        )
        for entry in iter_records(cache_dir)
        if entry.ts >= cutoff
    ]
    total = path.read_text(encoding='utf-8').count('\n')
    path.write_text(''.join(f'{line}\n' for line in kept), encoding='utf-8')
    return total - len(kept)
