"""Tests for cache storage: key generation, the v2 Parquet schema, and eviction."""

import hashlib
import locale
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

from tail_cw.cache.records import is_jsonl_message
from tail_cw.cache.storage import (
    METADATA_DIRNAME,
    LogCache,
    generate_cache_key,
    read_parquet_to_log_events,
    write_log_events_to_parquet,
)
from tests.factories import BASE_TIME, make_event, make_events

_SHORT_TTL = 0.05
"""Fractional TTL so expiry tests finish in milliseconds instead of seconds."""

_PAST_TTL = _SHORT_TTL * 3
"""Sleep long enough to clear _SHORT_TTL on a loaded CI machine."""

_START = datetime(2025, 1, 1, tzinfo=UTC)
_END = datetime(2025, 1, 2, tzinfo=UTC)


def _key(log_group: str = '/aws/lambda/fn', **kwargs) -> str:
    return generate_cache_key(log_group, _START, _END, **kwargs)


def test_generate_cache_key_is_deterministic_and_versioned():
    key = _key(log_stream_names=['stream-2', 'stream-1'], region_name='us-west-2')

    assert key == _key(log_stream_names=['stream-1', 'stream-2'], region_name='us-west-2')
    assert key.startswith('cache:v2:')


@pytest.mark.parametrize(
    'other',
    [
        {'log_group': '/aws/lambda/other'},
        {'region_name': 'us-west-2'},
        {'profile_name': 'read-prod'},
        {'log_stream_names': ['stream-1']},
    ],
)
def test_every_keyed_parameter_changes_the_key(other):
    assert _key(**other) != _key()


def test_the_window_changes_the_key():
    assert generate_cache_key('/aws/lambda/fn', _START, _END + timedelta(hours=1)) != _key()


@pytest.mark.parametrize(
    ('message', 'expected'),
    [
        ('{"level":"INFO","msg":"test"}', True),
        ('  {"key":"value"}', True),
        ('\t{"key":"value"}', True),
        ('2025-01-01T12:00:00Z {"k":1}', True),
        ('2025-01-01T12:00:00.123456Z {"k":1}', True),
        ('2025-01-01T12:00:00+00:00 {"k":1}', True),
        ('  2025-01-01T12:00:00Z   {"k":1}', True),
        ('Plain log message', False),
        ('[ERROR] message', False),
        ('2025-01-01T12:00:00Z Plain text', False),
        ('', False),
        ('   ', False),
    ],
)
def test_is_jsonl_message(message, expected):
    assert is_jsonl_message(message) is expected


@pytest.mark.parametrize(
    ('messages', 'expected_jsonl'),
    [
        (['plain one', 'plain two'], 0),
        (['{"level":"INFO"}', '{"level":"ERROR"}'], 2),
        (['plain', '{"level":"INFO"}', '{invalid json', '{"level":"ERROR"}'], 2),
    ],
)
def test_write_counts_the_events_it_parsed(fix_test_cache: Path, messages, expected_jsonl):
    output_path = fix_test_cache / 'counts.parquet'

    stats = write_log_events_to_parquet(make_events(messages), output_path)

    assert stats['total_events'] == len(messages)
    assert stats['jsonl_events'] == expected_jsonl
    assert stats['file_size_bytes'] > 0


def test_a_pretty_printed_payload_does_not_break_the_file(fix_test_cache: Path):
    """It is valid JSON, so it takes the parsed path, and its newlines would end the NDJSON line early."""
    message = '{\n  "level": "info",\n  "event": "multi line"\n}'
    output_path = fix_test_cache / 'pretty.parquet'

    stats = write_log_events_to_parquet(make_events([message]), output_path)

    assert stats['jsonl_events'] == 1
    assert [event.message for event in read_parquet_to_log_events(output_path)] == [
        '{"level":"info","event":"multi line"}',
    ]


@pytest.mark.parametrize(
    ('message', 'expected_path'),
    [
        ('{"level":"INFO","meta":{}}', 'parsed.meta'),
        ('{"level":"INFO","a":{"b":{}}}', 'parsed.a.b'),
    ],
)
def test_an_empty_payload_object_names_the_key_it_came_from(fix_test_cache: Path, message, expected_path):
    """Polars reports only the dtype, so the bare message cannot be acted on."""
    output_path = fix_test_cache / 'empty_struct.parquet'

    with pytest.raises(ValueError, match=f'Empty JSON object at {re.escape(expected_path)}'):
        write_log_events_to_parquet(make_events([message]), output_path)


def test_one_key_logged_as_two_scalar_types_says_so(fix_test_cache: Path):
    output_path = fix_test_cache / 'conflict.parquet'

    with pytest.raises(ValueError, match='more than one scalar type'):
        write_log_events_to_parquet(make_events(['{"level":"INFO","n":1}', '{"level":"INFO","n":true}']), output_path)


def test_write_rejects_an_empty_batch(fix_test_cache: Path):
    output_path = fix_test_cache / 'empty.parquet'

    with pytest.raises(ValueError, match='empty log events'):
        write_log_events_to_parquet([], output_path)

    assert not output_path.exists()


def test_the_schema_stores_a_json_line_once(fix_test_cache: Path):
    """A JSON event keeps only ``parsed``; the raw line would be the same bytes twice."""
    output_path = fix_test_cache / 'schema.parquet'

    write_log_events_to_parquet(make_events(['plain text', '{"level":"INFO","n":1}']), output_path)

    frame = pl.read_parquet(output_path)
    assert set(frame.columns) == {'log_group', 'log_stream', 'timestamp', 'ingestion_time', 'message', 'parsed'}
    assert frame['message'].to_list() == ['plain text', None]
    assert frame['timestamp'].dtype == pl.Datetime('us', 'UTC')
    assert frame['ingestion_time'].dtype == pl.Datetime('us', 'UTC')


def test_a_json_event_reads_back_as_compact_json(fix_test_cache: Path):
    """The text is rebuilt from ``parsed``, dropping keys the line never carried."""
    output_path = fix_test_cache / 'rebuilt.parquet'
    messages = ['{"level":"INFO","msg":"one"}', '{"level":"ERROR","msg":"two","extra":5}']

    write_log_events_to_parquet(make_events(messages), output_path)

    assert [event.message for event in read_parquet_to_log_events(output_path)] == messages


def test_events_are_stored_in_timestamp_order(fix_test_cache: Path):
    output_path = fix_test_cache / 'sorted.parquet'
    events = [
        make_event('third', timestamp=BASE_TIME + timedelta(seconds=2)),
        make_event('first', timestamp=BASE_TIME),
        make_event('second', timestamp=BASE_TIME + timedelta(seconds=1)),
    ]

    write_log_events_to_parquet(events, output_path)

    assert [event.message for event in read_parquet_to_log_events(output_path)] == ['first', 'second', 'third']


def test_write_keeps_fields_that_only_appear_late(fix_test_cache: Path):
    """A field absent, null, or differently typed early must not break or vanish.

    Sampling the first N rows to infer the schema panics the Parquet writer on a
    null-then-string key, fails to parse an int-then-string key, and silently drops a key
    that first appears past the sample, so the whole file is scanned instead.
    """
    messages = ['{"kept": null, "widened": 1}'] * 1200 + [
        '{"kept": "text", "widened": 2}',
        '{"kept": "text", "widened": "text"}',
        '{"kept": "text", "widened": 3, "appeared": "text"}',
    ]
    output_path = fix_test_cache / 'late_fields.parquet'

    stats = write_log_events_to_parquet(make_events(messages), output_path)

    assert stats['total_events'] == len(messages)
    parsed = pl.read_parquet(output_path)['parsed']
    assert set(parsed.struct.fields) == {'kept', 'widened', 'appeared'}
    assert parsed.struct.field('kept').drop_nulls().to_list() == ['text', 'text', 'text']
    assert parsed.struct.field('appeared').drop_nulls().to_list() == ['text']


def test_write_reports_progress(fix_test_cache: Path):
    calls: list[tuple[int, int, str]] = []
    events = make_events(f'{{"idx": {index}}}' for index in range(2100))

    stats = write_log_events_to_parquet(
        events,
        fix_test_cache / 'progress.parquet',
        progress_callback=lambda *update: calls.append(update),
    )

    assert stats['total_events'] == 2100
    assert {status for _, _, status in calls} == {'Parsing JSONL...', 'Converting to Parquet...'}
    assert any(total == -1 for _, total, _ in calls)
    assert any(total == 2100 for _, total, _ in calls)


def test_read_round_trips_every_field(fix_test_cache: Path):
    originals = [
        make_event(
            'with ingestion time',
            log_group='/aws/lambda/fn1',
            log_stream='stream-1',
            timestamp=datetime(2025, 1, 1, 12, 0, 0, 123456, tzinfo=UTC),
        ),
        make_event(
            'without ingestion time',
            log_group='/aws/lambda/fn2',
            log_stream='stream-2',
            timestamp=datetime(2025, 1, 2, 13, 30, 45, 654321, tzinfo=UTC),
            ingestion_offset=None,
        ),
    ]
    output_path = fix_test_cache / 'round_trip.parquet'
    write_log_events_to_parquet(originals, output_path)

    assert list(read_parquet_to_log_events(output_path)) == originals


def test_read_rejects_a_missing_file(fix_test_cache: Path):
    with pytest.raises(FileNotFoundError, match='Parquet file not found'):
        list(read_parquet_to_log_events(fix_test_cache / 'nonexistent.parquet'))


def test_log_cache_creates_its_directories(fix_test_cache: Path):
    cache_dir = fix_test_cache / 'cache_init'

    with LogCache(cache_dir) as cache:
        assert cache._parquet_dir == cache_dir / 'parquet'
        assert cache._parquet_dir.exists()


def test_log_cache_write_read_and_exists(fix_test_cache: Path):
    with LogCache(fix_test_cache / 'basic') as cache:
        key = _key()
        assert cache.exists(key) is False

        stats = cache.write(make_events(['first', 'second', 'third']), key)

        assert stats['total_events'] == 3
        assert cache.exists(key) is True
        assert [event.message for event in cache.read(key)] == ['first', 'second', 'third']
        assert list(cache.read('nonexistent-key')) == []
        assert cache.exists('nonexistent-key') is False


def test_log_cache_reports_progress(fix_test_cache: Path):
    calls: list[tuple[int, int, str]] = []
    events = make_events(f'{{"value": {index}}}' for index in range(1100))

    with LogCache(fix_test_cache / 'progress') as cache:
        stats = cache.write(events, _key(), progress_callback=lambda *update: calls.append(update))

    assert stats['total_events'] == 1100
    assert {status for _, _, status in calls} == {'Parsing JSONL...', 'Converting to Parquet...'}


@pytest.mark.parametrize(
    ('cache_kwargs', 'write_kwargs', 'expected'),
    [
        ({}, {'ttl_seconds': _SHORT_TTL}, False),
        ({'default_ttl_seconds': _SHORT_TTL}, {}, False),
        ({'default_ttl_seconds': _SHORT_TTL}, {'ttl_seconds': 10}, True),
    ],
    ids=['explicit-ttl-expires', 'default-ttl-expires', 'explicit-ttl-overrides-default'],
)
def test_ttl_decides_what_survives(fix_test_cache: Path, cache_kwargs, write_kwargs, expected):
    with LogCache(fix_test_cache / 'ttl', **cache_kwargs) as cache:
        key = _key()
        cache.write([make_event()], key, **write_kwargs)
        assert cache.exists(key) is True

        time.sleep(_PAST_TTL)
        cache.evict_expired()

        assert cache.exists(key) is expected


def _incompressible(index: int) -> str:
    """Hex digests, so ZSTD cannot shrink the batch below the eviction threshold."""
    return hashlib.blake2b(str(index).encode(), digest_size=64).hexdigest()


def test_fifo_eviction_drops_the_oldest_entries(fix_test_cache: Path):
    with LogCache(fix_test_cache / 'fifo', size_limit_mb=1) as cache:
        keys = []
        for index in range(10):
            key = _key(f'/aws/lambda/fn{index}')
            keys.append(key)
            cache.write(make_events(_incompressible(index * 3000 + row) for row in range(3000)), key)
            # Distinct creation times, which is what the FIFO order reads.
            time.sleep(0.01)

        assert sum(path.stat().st_size for path in cache._parquet_dir.glob('*.parquet')) <= 1024 * 1024
        assert not all(cache.exists(key) for key in keys[:5]), 'the oldest entries should be evicted first'
        assert cache.exists(keys[-1]), 'the newest entry should survive'


def test_clear_removes_metadata_and_files(fix_test_cache: Path):
    with LogCache(fix_test_cache / 'clear') as cache:
        keys = [_key(f'/aws/lambda/fn{index}') for index in range(3)]
        for key in keys:
            cache.write([make_event()], key)

        cache.clear()

        assert not any(cache.exists(key) for key in keys)
        assert list(cache._parquet_dir.glob('*.parquet')) == []


def test_a_file_with_no_metadata_entry_is_reclaimed(fix_test_cache: Path):
    with LogCache(fix_test_cache / 'orphan') as cache:
        key = _key()
        cache.write([make_event()], key)
        cache._metadata.delete(key)

        assert cache.evict_expired() == 1
        assert list(cache._parquet_dir.glob('*.parquet')) == []


def test_separate_keys_stay_independent(fix_test_cache: Path):
    with LogCache(fix_test_cache / 'independent') as cache:
        for index in range(5):
            cache.write([make_event(f'Message {index}')], _key(f'/aws/lambda/fn{index}'))

        for index in range(5):
            assert [event.message for event in cache.read(_key(f'/aws/lambda/fn{index}'))] == [f'Message {index}']
        assert len(list(cache._parquet_dir.glob('*.parquet'))) == 5


@pytest.mark.parametrize(
    'message',
    [
        '{invalid json structure',
        'Message with unicode: 你好世界',
        'Message with newlines:\nLine 1\nLine 2',
        'Message with quotes: "test" and \'test\'',
    ],
)
def test_text_that_is_not_json_survives_the_round_trip(fix_test_cache: Path, message):
    with LogCache(fix_test_cache / 'text') as cache:
        key = _key('/aws/lambda/my-function_v2-test')
        cache.write([make_event(message)], key)

        assert [event.message for event in cache.read(key)] == [message]


def test_unicode_message_survives_a_non_utf8_preferred_encoding(fix_test_cache: Path, monkeypatch):
    monkeypatch.setattr(locale, 'getpreferredencoding', lambda do_setlocale=True: 'ascii')  # noqa: ARG005
    message = 'Message with unicode: 你好世界'
    with LogCache(fix_test_cache / 'text') as cache:
        key = _key('/aws/lambda/my-function_v2-test')
        cache.write([make_event(message)], key)

        assert [event.message for event in cache.read(key)] == [message]


def test_microsecond_precision_survives_the_round_trip(fix_test_cache: Path):
    timestamp = datetime(2025, 1, 1, 12, 30, 45, 123456, tzinfo=UTC)
    with LogCache(fix_test_cache / 'precision') as cache:
        key = _key()
        cache.write([make_event(timestamp=timestamp, ingestion_offset=timedelta(microseconds=654321))], key)

        cached = next(iter(cache.read(key)))
        assert cached.timestamp == timestamp
        assert cached.ingestion_time == timestamp + timedelta(microseconds=654321)


def test_a_large_batch_reads_back_completely(fix_test_cache: Path):
    with LogCache(fix_test_cache / 'large') as cache:
        key = _key()
        stats = cache.write(make_events(f'Log message number {index}' for index in range(10000)), key)

        assert stats['total_events'] == 10000
        assert sum(1 for _ in cache.read(key)) == 10000


def test_metadata_store_does_not_use_pickle(fix_test_cache: Path):
    """The metadata store serializes with JSON, closing CVE-2025-69872.

    diskcache pickles by default, so write access to the cache directory means
    code execution when we read it back, and upstream has no fix. This asserts
    the JSONDisk swap holds.
    """
    cache_dir = fix_test_cache / 'nopickle'
    with LogCache(cache_dir) as cache:
        cache.write_payload('preview:v1:probe', {'event_count': 7, 'note': 'hello'})
        assert cache.read_payload('preview:v1:probe') == {'event_count': 7, 'note': 'hello'}

    stored = b''.join(path.read_bytes() for path in (cache_dir / METADATA_DIRNAME).rglob('*') if path.is_file())
    for opcode in (b'\x80\x04', b'\x80\x05', b'__reduce__', b'copy_reg'):
        assert opcode not in stored


def test_metadata_directory_is_versioned_away_from_the_pickle_store(fix_test_cache: Path):
    """A pickle-era store is left alone rather than read back as empty.

    Reading it under JSONDisk yields None for every key, which would make each
    cached Parquet file look orphaned and get swept on the first write.
    """
    assert METADATA_DIRNAME != 'metadata'

    cache_dir = fix_test_cache / 'versioned'
    with LogCache(cache_dir) as cache:
        cache.write_payload('preview:v1:x', {'event_count': 1})

    assert (cache_dir / METADATA_DIRNAME).is_dir()
    assert not (cache_dir / 'metadata').exists()


def test_entries_from_an_older_schema_are_reclaimed(fix_test_cache: Path):
    """A v1 file can never be read again, so it must not wait for FIFO eviction."""
    cache_dir = fix_test_cache / 'superseded'
    with LogCache(cache_dir) as cache:
        cache.write([make_event()], _key())
        superseded = next(iter(cache._parquet_dir.glob('*.parquet')))
        cache._metadata.set('cache:v1:legacy', (str(superseded), superseded.stat().st_size))

    with LogCache(cache_dir) as cache:
        assert cache._metadata.get('cache:v1:legacy') is None
        assert not superseded.exists()


def test_status_counts_what_is_on_disk_against_the_limit(tmp_path):
    """The complaint this answers is not knowing either number."""
    with LogCache(tmp_path / 'cache', size_limit_mb=1, default_ttl_seconds=90) as cache:
        empty = cache.status()
        assert empty.files == 0
        assert empty.entries == 0
        assert empty.oldest is None
        assert not empty.fraction_used
        assert empty.bytes_limit == 1024 * 1024
        assert empty.default_ttl_seconds == 90

        cache.write(make_events(['a', 'b', 'c']), _key())
        status = cache.status()

        assert status.files == 1
        assert status.entries == 1
        assert status.bytes_used > 0
        assert status.orphan_files == 0
        assert status.stale_entries == 0
        assert status.oldest is not None
        assert status.newest is not None
        assert 0 < status.fraction_used < 1


def test_status_names_a_file_no_entry_points_at(tmp_path):
    """A crash between writing the Parquet and committing the entry leaves one behind."""
    with LogCache(tmp_path / 'cache') as cache:
        cache.write(make_events(['a', 'b']), _key())
        (cache.get_parquet_path(_key()) or tmp_path).parent.joinpath('cache_v2_stray.parquet').write_bytes(b'x')

        status = cache.status()

        assert status.files == 2
        assert status.entries == 1
        assert status.orphan_files == 1


def test_status_names_an_entry_whose_file_is_gone(tmp_path):
    with LogCache(tmp_path / 'cache') as cache:
        cache.write(make_events(['a', 'b']), _key())
        path = cache.get_parquet_path(_key())
        assert path is not None
        path.unlink()

        status = cache.status()

        assert status.files == 0
        assert status.stale_entries == 1
        assert status.entries == 1
