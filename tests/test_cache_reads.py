"""Tests for the append-only read record behind ``cache status`` and eviction."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tail_cw.cache import reads
from tail_cw.cache.storage import LogCache, generate_cache_key
from tests.factories import make_events

_START = datetime(2025, 1, 1, tzinfo=UTC)
_END = datetime(2025, 1, 2, tzinfo=UTC)
_NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


def _key(log_group: str = '/aws/lambda/fn', **kwargs) -> str:
    return generate_cache_key(log_group, _START, _END, **kwargs)


def test_a_recorded_lookup_round_trips(tmp_path: Path):
    reads.record(tmp_path, op='hit', key='cache:v2:abc', log_group='/g', bytes_served=42, now=_NOW)
    reads.record(tmp_path, op='miss', key='cache:v2:def', now=_NOW)

    entries = list(reads.iter_records(tmp_path))

    assert [(entry.op, entry.key) for entry in entries] == [('hit', 'cache:v2:abc'), ('miss', 'cache:v2:def')]
    assert entries[0].log_group == '/g'
    assert entries[0].bytes_served == 42
    assert entries[1].log_group is None


def test_a_garbage_line_is_skipped_not_fatal(tmp_path: Path):
    (tmp_path / reads.READS_FILENAME).write_text(
        'not json\n{"ts":"bad"}\n{"ts":"2026-10-21T00:00:00+00:00","op":"hit","key":"k"}\n'
    )

    entries = list(reads.iter_records(tmp_path))

    assert [entry.key for entry in entries] == ['k']


def test_summarize_counts_the_window_and_flags_unread_bytes(tmp_path: Path):
    live = {'a': 100, 'b': 200, 'c': 50}
    old = _NOW - timedelta(days=30)
    reads.record(tmp_path, op='hit', key='a', bytes_served=100, now=_NOW - timedelta(days=1))
    reads.record(tmp_path, op='hit', key='a', bytes_served=100, now=_NOW - timedelta(days=10))
    reads.record(tmp_path, op='miss', key='d', now=_NOW - timedelta(hours=1))
    reads.record(tmp_path, op='hit', key='b', bytes_served=200, now=old)  # outside the 7d window, still a read
    reads.record(tmp_path, op='write', key='c', bytes_served=50, now=_NOW - timedelta(days=2))

    stats = reads.summarize(tmp_path, live, since=_NOW - timedelta(days=7))

    assert stats.hits == 1
    assert stats.misses == 1
    assert stats.hit_rate == pytest.approx(0.5)
    assert stats.bytes_served == 100
    assert stats.bytes_fetched == 50
    # 'c' was written but never read back; 'b' was last read 10 days ago.
    assert stats.bytes_never_read == 50
    assert stats.oldest_read == old


def test_compact_drops_old_records_only_when_oversized(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(reads, '_COMPACT_ABOVE_BYTES', 10)
    reads.record(tmp_path, op='hit', key='old', now=_NOW - timedelta(days=40))
    reads.record(tmp_path, op='hit', key='new', now=_NOW)

    dropped = reads.compact(tmp_path, now=_NOW)

    assert dropped == 1
    assert [entry.key for entry in reads.iter_records(tmp_path)] == ['new']


def test_cache_write_and_lookup_fill_the_read_log(fix_test_cache: Path):
    with LogCache(fix_test_cache / 'c') as cache:
        key = _key()
        cache.write(make_events(['a', 'b']), key, log_group='/aws/lambda/fn')
        cache.get_parquet_path(key)
        cache.get_parquet_path(_key(log_group='/aws/lambda/other'), log_group='/aws/lambda/other')

    ops = [entry.op for entry in reads.iter_records(fix_test_cache / 'c')]
    assert ops == ['write', 'hit', 'miss']
    entries = list(reads.iter_records(fix_test_cache / 'c'))
    assert entries[1].bytes_served > 0
    assert entries[2].log_group == '/aws/lambda/other'


def test_status_reports_read_activity_and_top_groups(fix_test_cache: Path):
    with LogCache(fix_test_cache / 'c') as cache:
        cache.write(make_events(['a']), _key(), log_group='/aws/lambda/fn')
        cache.write(make_events(['b'] * 400), _key(log_group='/aws/lambda/big'), log_group='/aws/lambda/big')
        cache.get_parquet_path(_key())

        status = cache.status()

    assert status.reads is not None
    assert status.reads.hits == 1
    assert status.reads.misses == 0
    assert status.reads.hit_rate == pytest.approx(1.0)
    assert status.top_groups[0][0] == '/aws/lambda/big'
    # The big group's segment was never read back; fn's was.
    assert status.bytes_used > 0
    unread = dict(status.top_groups)
    assert status.reads.bytes_never_read == unread['/aws/lambda/big']
