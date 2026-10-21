"""Tests for the disk budget, the degrade ladder, and the read-age TTL."""

import json
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tail_cw.aws.events import LogEvent
from tail_cw.cache import reads
from tail_cw.cache.storage import DiskBudgetExceededError, LogCache, generate_cache_key
from tail_cw.cli import resolve_cache_ttl, run_cli
from tail_cw.config import TailCWConfig
from tail_cw.query.engine import query_parquet_file
from tests.factories import make_event, make_events
from tests.test_cache import _incompressible

_START = datetime(2025, 1, 1, tzinfo=UTC)
_END = datetime(2025, 1, 2, tzinfo=UTC)
_NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


def _key(log_group: str = '/aws/lambda/fn', start: datetime = _START, end: datetime = _END, **kwargs) -> str:
    return generate_cache_key(log_group, start, end, **kwargs)


def test_a_sparse_write_answers_the_query_and_persists_nothing(fix_test_cache: Path):
    with LogCache(fix_test_cache / 'c') as cache:
        path = cache.write_sparse(make_events(['a', 'b']), log_group='/g')

        assert path.exists()
        assert path.parent == fix_test_cache / 'c' / 'staging'
        assert [row['message'] for row in query_parquet_file(path)] == ['a', 'b']
        cache.drop_sparse()

        assert not path.exists()
        assert list((fix_test_cache / 'c' / 'parquet').glob('*.parquet')) == []


def test_prepare_write_persists_when_everything_fits(fix_test_cache: Path):
    with LogCache(fix_test_cache / 'c') as cache:
        persist, notice = cache.prepare_write('/g', max_added=None, added=0)

    assert persist is True
    assert notice is None


def test_prepare_write_goes_sparse_under_the_free_space_floor(fix_test_cache: Path, monkeypatch):
    with LogCache(fix_test_cache / 'c', min_free_mb=5120) as cache:
        monkeypatch.setattr(cache, '_free_bytes', lambda: 4 * 1024**3)
        persist, notice = cache.prepare_write('/g', max_added=None, added=0)

    assert persist is False
    assert notice is not None
    assert 'under the' in notice
    assert 'floor' in notice


def test_prepare_write_refuses_when_the_floor_cannot_hold_a_sparse_file(fix_test_cache: Path, monkeypatch):
    with LogCache(fix_test_cache / 'c', min_free_mb=5120) as cache:
        monkeypatch.setattr(cache, '_free_bytes', lambda: 200 * 1024**2)
        with pytest.raises(DiskBudgetExceededError, match='narrow the window'):
            cache.prepare_write('/g', max_added=None, added=0)


def test_prepare_write_refuses_a_segment_bigger_than_the_command_ceiling(fix_test_cache: Path, monkeypatch):
    with LogCache(fix_test_cache / 'c', min_free_mb=5120) as cache:
        monkeypatch.setattr(cache, '_estimate_segment_bytes', lambda _group: 900 * 1024**2)
        with pytest.raises(DiskBudgetExceededError, match='--max-disk'):
            cache.prepare_write('/g', max_added=500 * 1024**2, added=0)


def test_eviction_drops_the_least_recently_read(fix_test_cache: Path):
    with LogCache(fix_test_cache / 'c', size_limit_mb=1) as cache:
        old_key, new_key = _key('/g/old'), _key('/g/new')
        cache.write(make_events(['a']), old_key, log_group='/g/old')
        cache.write(make_events(['b']), new_key, log_group='/g/new')
        cache._written.clear()  # stand-ins for a previous command's entries
        # /g/old was read two days ago; /g/new was never read, so its activity is
        # its write time, which is newer and should outlive the old hit.
        reads.record(cache._cache_dir, op='hit', key=old_key, log_group='/g/old', now=_NOW - timedelta(days=2))
        new_path = cache.get_parquet_path(new_key, record=False)
        assert new_path is not None
        new_size = new_path.stat().st_size

        # Ask for just enough room that exactly one file has to go.
        cache._evict_for(cache._size_limit_bytes - new_size, now=_NOW)

        assert not cache.exists(old_key)
        assert cache.exists(new_key)


def test_a_recently_read_entry_survives_eviction(fix_test_cache: Path):
    with LogCache(fix_test_cache / 'c', size_limit_mb=1) as cache:
        key = _key()
        cache.write(make_events(['a']), key, log_group='/g')
        reads.record(cache._cache_dir, op='hit', key=key, now=_NOW - timedelta(hours=1))
        used = sum(path.stat().st_size for path in cache._parquet_dir.glob('*.parquet'))

        evicted = cache._evict_for(used + 1, now=_NOW)

        assert evicted == 0
        assert cache.exists(key)


def test_the_read_ttl_expires_from_the_last_read_not_the_write(fix_test_cache: Path):
    with LogCache(fix_test_cache / 'c', default_ttl_seconds=3600) as cache:
        key = _key()
        cache.write(make_events(['a']), key, log_group='/g')
        # Written 3 hours ago, read 30 minutes ago: a write-age TTL would evict,
        # a read-age TTL must not.
        reads.record(cache._cache_dir, op='hit', key=key, now=_NOW - timedelta(minutes=30))

        cache.maintain(now=_NOW)

        assert cache.exists(key)

        # And without the read, an entry untouched for over an hour expires.
        cache.maintain(now=_NOW + timedelta(hours=2))
        assert not cache.exists(key)


def test_a_stale_staging_file_is_swept(fix_test_cache: Path):
    staging = fix_test_cache / 'c' / 'staging'
    staging.mkdir(parents=True)
    stale = staging / 'old.jsonl'
    stale.write_text('x')
    old = (datetime.now(UTC) - timedelta(hours=2)).timestamp()
    os.utime(stale, (old, old))

    with LogCache(fix_test_cache / 'c') as cache:
        swept = cache._sweep_staging(datetime.now(UTC))

    assert swept == 1
    assert not stale.exists()


def test_the_group_quota_evicts_only_the_over_quota_group(fix_test_cache: Path):
    with LogCache(fix_test_cache / 'c', max_group_mb=1) as cache:
        big_old = _key('/aws/lambda/big', start=_START, end=_START + timedelta(hours=1))
        big_new = _key('/aws/lambda/big', start=_START + timedelta(hours=1), end=_END)
        small = _key('/aws/lambda/small')
        cache.write(make_events(['a']), big_old, log_group='/aws/lambda/big')
        cache.write(make_events(['b']), big_new, log_group='/aws/lambda/big')
        cache.write(make_events(['c']), small, log_group='/aws/lambda/small')
        cache._written.clear()  # stand-ins for a previous command's entries
        # Both big entries together exceed the quota but either fits alone; make
        # big_new the newer read so big_old is the one to go.
        later = datetime.now(UTC) + timedelta(hours=1)
        reads.record(cache._cache_dir, op='hit', key=big_new, log_group='/aws/lambda/big', now=later)
        big_path = cache.get_parquet_path(big_new, record=False)
        assert big_path is not None
        cache._max_group_bytes = big_path.stat().st_size

        cache._enforce_group_quota()

        assert not cache.exists(big_old)
        assert cache.exists(big_new)
        assert cache.exists(small)


def test_an_oversized_segment_is_answered_sparse_not_stored(fix_test_cache: Path, monkeypatch):
    with LogCache(fix_test_cache / 'c', max_segment_mb=1) as cache:
        monkeypatch.setattr(cache, '_estimate_segment_bytes', lambda _group: 5 * 1024**2)
        persist, notice = cache.prepare_write('/g', max_added=None, added=0)

    assert persist is False
    assert notice == 'cache: sparse (segment exceeds the max_segment_mb cap), not persisting'


def test_an_eviction_scans_the_metadata_once(fix_test_cache: Path, monkeypatch):
    with LogCache(fix_test_cache / 'c', size_limit_mb=1) as cache:
        keys = [_key(f'/aws/lambda/fn{index}') for index in range(6)]
        for key in keys:
            cache.write(make_events(['x']), key, log_group='/aws/lambda/fn')
        scans = 0
        real_iterkeys = cache._metadata.iterkeys

        def counting_iterkeys(*args, **kwargs):
            nonlocal scans
            scans += 1
            return real_iterkeys(*args, **kwargs)

        monkeypatch.setattr(cache._metadata, 'iterkeys', counting_iterkeys)
        cache._evict_for(cache._size_limit_bytes)

    assert scans == 1


def test_prepare_write_does_not_rescan_per_segment(fix_test_cache: Path, monkeypatch):
    """A wide fetch calls prepare_write once per segment; the scan must not repeat."""
    with LogCache(fix_test_cache / 'c', size_limit_mb=1) as cache:
        scans = 0
        real_iterkeys = cache._metadata.iterkeys

        def counting_iterkeys(*args, **kwargs):
            nonlocal scans
            scans += 1
            return real_iterkeys(*args, **kwargs)

        monkeypatch.setattr(cache._metadata, 'iterkeys', counting_iterkeys)
        for _ in range(4):
            cache.prepare_write('/g', max_added=None, added=0)

    assert scans == 1  # the one-time seed of the size index


def test_maintain_scans_the_metadata_once(fix_test_cache: Path, monkeypatch):
    with LogCache(fix_test_cache / 'c', default_ttl_seconds=60, max_group_mb=1) as cache:
        cache.write(make_events(['a']), _key(), log_group='/g')
        scans = 0
        real_iterkeys = cache._metadata.iterkeys

        def counting_iterkeys(*args, **kwargs):
            nonlocal scans
            scans += 1
            return real_iterkeys(*args, **kwargs)

        monkeypatch.setattr(cache._metadata, 'iterkeys', counting_iterkeys)
        cache.maintain()

    assert scans == 1


def test_eviction_never_drops_a_segment_this_command_wrote(fix_test_cache: Path):
    """A file the pending answer still names must outlive the eviction pass."""
    with LogCache(fix_test_cache / 'c', size_limit_mb=1) as cache:
        old_key, new_key = _key('/g/old'), _key('/g/new')
        cache.write(make_events(_incompressible(i) for i in range(3000)), old_key, log_group='/g')
        cache._written.clear()  # old_key stands in for a previous command
        cache.write(make_events(_incompressible(i) for i in range(3000)), new_key, log_group='/g')
        new_path = cache.get_parquet_path(new_key, record=False)

        cache._evict_for(cache._size_limit_bytes, now=_NOW)

        assert not cache.exists(old_key)
        assert new_path is not None
        assert new_path.exists()


def _write_config(tmp_path: Path, **cache_settings: int | str) -> Path:
    config = tmp_path / 'config.toml'
    lines = '\n'.join(f'{key} = {value}' for key, value in cache_settings.items())
    cache_dir = (tmp_path / 'cache').as_posix()
    config.write_text(f'[cache]\ncache_dir = "{cache_dir}"\n{lines}\n', encoding='utf-8')
    return config


class _FakeFetcher:
    def __init__(self, events) -> None:
        self._events = events
        self.calls = 0

    async def __call__(self, _client, _group, start_time, end_time, **_kwargs) -> AsyncIterator[LogEvent]:
        self.calls += 1
        for event in self._events:
            if start_time <= event.timestamp < end_time:
                yield event


def test_export_logs_with_cache_ttl_zero_stays_sparse(tmp_path, capsys):
    config = _write_config(tmp_path)
    events = [make_event('{"level":"INFO"}', timestamp=datetime.now(UTC) - timedelta(minutes=1))]

    result = run_cli(
        ['export', 'logs', '/g', '--start', '15m', '--cache-ttl', '0', '--config', str(config)],
        None,
        fetch_events=_FakeFetcher(events),
        is_tty=False,
    )

    assert result == 0
    assert json.loads(capsys.readouterr().out.splitlines()[0])['message'] == '{"level":"INFO"}'
    assert list((tmp_path / 'cache' / 'parquet').glob('*.parquet')) == []
    assert list((tmp_path / 'cache' / 'staging').iterdir()) == []


def test_export_logs_low_impact_says_what_it_throttled(tmp_path, capsys):
    config = _write_config(tmp_path)
    events = [make_event('{"level":"INFO"}', timestamp=datetime.now(UTC) - timedelta(minutes=1))]

    result = run_cli(
        ['export', 'logs', '/g', '--start', '15m', '--low-impact', '--config', str(config)],
        None,
        fetch_events=_FakeFetcher(events),
        is_tty=False,
    )

    assert result == 0
    assert 'low-impact' in capsys.readouterr().err


def test_export_logs_rejects_a_bad_cache_ttl(tmp_path, capsys):
    config = _write_config(tmp_path)

    result = run_cli(
        ['export', 'logs', '/g', '--cache-ttl', 'bogus', '--config', str(config)],
        None,
        fetch_events=_FakeFetcher([make_event()]),
        is_tty=False,
    )

    assert result != 0
    assert 'cache-ttl' in capsys.readouterr().err


def test_export_logs_stream_flag_narrows_the_read(tmp_path, capsys):
    config = _write_config(tmp_path)
    now = datetime.now(UTC)
    events = [
        make_event('{"level":"INFO","n":1}', timestamp=now - timedelta(minutes=2), log_stream='build-1'),
        make_event('{"level":"INFO","n":2}', timestamp=now - timedelta(minutes=1), log_stream='build-2'),
    ]

    result = run_cli(
        ['export', 'logs', '/g', '--start', '15m', '--stream', 'build-1', '--config', str(config)],
        None,
        fetch_events=_FakeFetcher(events),
        is_tty=False,
    )

    assert result == 0
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 1
    assert json.loads(out[0])['log_stream'] == 'build-1'


def test_export_logs_stream_prefix_narrows_the_read(tmp_path, capsys):
    config = _write_config(tmp_path)
    now = datetime.now(UTC)
    events = [
        make_event('{"level":"INFO","n":1}', timestamp=now - timedelta(minutes=2), log_stream='build-1'),
        make_event('{"level":"INFO","n":2}', timestamp=now - timedelta(minutes=1), log_stream='other-9'),
    ]

    result = run_cli(
        ['export', 'logs', '/g', '--start', '15m', '--stream-prefix', 'build-', '--config', str(config)],
        None,
        fetch_events=_FakeFetcher(events),
        is_tty=False,
    )

    assert result == 0
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 1
    assert json.loads(out[0])['log_stream'] == 'build-1'


def test_a_zero_match_quoted_term_names_case_sensitivity(tmp_path, capsys):
    config = _write_config(tmp_path)

    result = run_cli(
        ['export', 'logs', '/g', '--start', '15m', '--filter', '"dev-11423"', '--config', str(config)],
        None,
        fetch_events=_FakeFetcher([]),
        is_tty=False,
    )

    assert result == 0
    err = capsys.readouterr().err
    assert 'case-sensitive' in err
    assert '%(?i)' in err


def test_a_zero_match_unquoted_term_gets_no_hint(tmp_path, capsys):
    config = _write_config(tmp_path)

    result = run_cli(
        ['export', 'logs', '/g', '--start', '15m', '--filter', 'error', '--config', str(config)],
        None,
        fetch_events=_FakeFetcher([]),
        is_tty=False,
    )

    assert result == 0
    assert 'case-sensitive' not in capsys.readouterr().err


def test_resolve_cache_ttl_spells():
    config = TailCWConfig()
    assert resolve_cache_ttl('never', config) is None
    assert resolve_cache_ttl('-1', config) is None
    assert resolve_cache_ttl('0', config) == pytest.approx(0.0)
    assert resolve_cache_ttl('7d', config) == pytest.approx(7 * 86400.0)
    with pytest.raises(ValueError, match='cache-ttl'):
        resolve_cache_ttl('sometimes', config)
