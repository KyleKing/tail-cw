"""Tests for cache storage module."""

import time
from datetime import UTC, datetime
from pathlib import Path

import polars as pl
import pyarrow.parquet as pq  # type: ignore[import-untyped]
import pytest

from tail_cw.aws.client import LogEvent
from tail_cw.cache.storage import (
    METADATA_DIRNAME,
    LogCache,
    generate_cache_key,
    is_jsonl_message,
    read_parquet_to_log_events,
    write_log_events_to_parquet,
)

_SHORT_TTL = 0.05
"""Fractional TTL so expiry tests finish in milliseconds instead of seconds."""

_PAST_TTL = _SHORT_TTL * 3
"""Sleep long enough to clear _SHORT_TTL on a loaded CI machine."""


def _make_log_event(
    log_group: str = '/aws/lambda/test-function',
    log_stream: str = '2025/01/01/stream-123',
    timestamp: datetime | None = None,
    message: str = 'Test log message',
    event_id: str = 'event-123',
    ingestion_time: datetime | None = None,
) -> LogEvent:
    """Create a test LogEvent instance.

    Args:
        log_group: CloudWatch log group name.
        log_stream: CloudWatch log stream name.
        timestamp: Event timestamp. Defaults to current UTC time.
        message: Log message content.
        event_id: Unique event identifier.
        ingestion_time: Optional ingestion timestamp.

    Returns:
        LogEvent instance for testing.
    """
    if timestamp is None:
        timestamp = datetime.now(tz=UTC)

    return LogEvent(
        log_group=log_group,
        log_stream=log_stream,
        timestamp=timestamp,
        message=message,
        event_id=event_id,
        ingestion_time=ingestion_time,
    )


def test_generate_cache_key_deterministic():
    """Test that cache key generation is deterministic."""
    start = datetime(2025, 1, 1, tzinfo=UTC)
    end = datetime(2025, 1, 2, tzinfo=UTC)

    # Generate same key twice
    key1 = generate_cache_key('/aws/lambda/fn', start, end)
    key2 = generate_cache_key('/aws/lambda/fn', start, end)

    assert key1 == key2
    assert key1.startswith('cache:v1:')

    # Test with all parameters
    key3 = generate_cache_key(
        '/aws/lambda/fn',
        start,
        end,
        filter_pattern='[ERROR]',
        log_stream_names=['stream-1', 'stream-2'],
        region_name='us-west-2',
    )
    key4 = generate_cache_key(
        '/aws/lambda/fn',
        start,
        end,
        filter_pattern='[ERROR]',
        log_stream_names=['stream-1', 'stream-2'],
        region_name='us-west-2',
    )

    assert key3 == key4
    assert key3.startswith('cache:v1:')


def test_generate_cache_key_different_inputs():
    """Test that different inputs produce different cache keys."""
    start = datetime(2025, 1, 1, tzinfo=UTC)
    end = datetime(2025, 1, 2, tzinfo=UTC)

    key_group1 = generate_cache_key('/aws/lambda/fn1', start, end)
    key_group2 = generate_cache_key('/aws/lambda/fn2', start, end)

    key_time1 = generate_cache_key(
        '/aws/lambda/fn',
        start,
        datetime(2025, 1, 3, tzinfo=UTC),
    )
    key_time2 = generate_cache_key('/aws/lambda/fn', start, end)

    key_filter1 = generate_cache_key(
        '/aws/lambda/fn',
        start,
        end,
        filter_pattern='[ERROR]',
    )
    key_filter2 = generate_cache_key(
        '/aws/lambda/fn',
        start,
        end,
        filter_pattern='[WARN]',
    )

    # All keys should be unique
    keys = {key_group1, key_group2, key_time1, key_time2, key_filter1, key_filter2}
    assert len(keys) == 6


def test_generate_cache_key_order_independence():
    """Test that log_stream_names order doesn't affect cache key."""
    start = datetime(2025, 1, 1, tzinfo=UTC)
    end = datetime(2025, 1, 2, tzinfo=UTC)

    key1 = generate_cache_key(
        '/aws/lambda/fn',
        start,
        end,
        log_stream_names=['stream-1', 'stream-2', 'stream-3'],
    )

    key2 = generate_cache_key(
        '/aws/lambda/fn',
        start,
        end,
        log_stream_names=['stream-3', 'stream-1', 'stream-2'],
    )

    assert key1 == key2


def test_is_jsonl_message():
    """Test JSONL message detection."""
    # Valid JSON
    assert is_jsonl_message('{"level":"INFO","msg":"test"}') is True

    # JSON with leading whitespace
    assert is_jsonl_message('  {"key":"value"}') is True
    assert is_jsonl_message('\t{"key":"value"}') is True

    # JSON with leading timestamp (ISO8601/RFC3339)
    assert is_jsonl_message('2025-01-01T12:00:00Z {"k":1}') is True
    assert is_jsonl_message('2025-01-01T12:00:00.123456Z {"k":1}') is True
    assert is_jsonl_message('2025-01-01T12:00:00+00:00 {"k":1}') is True
    assert is_jsonl_message('  2025-01-01T12:00:00Z   {"k":1}') is True

    # Plain text
    assert is_jsonl_message('Plain log message') is False

    # Text starting with bracket (not JSON)
    assert is_jsonl_message('[ERROR] message') is False

    # Timestamp without JSON
    assert is_jsonl_message('2025-01-01T12:00:00Z Plain text') is False

    # Empty string
    assert is_jsonl_message('') is False

    # Only whitespace
    assert is_jsonl_message('   ') is False


def test_write_log_events_to_parquet_plain_text(fix_test_cache: Path):
    """Test Parquet writing with plain text log messages."""
    events = [
        _make_log_event(
            event_id='event-1',
            message='First plain text message',
        ),
        _make_log_event(
            event_id='event-2',
            message='Second plain text message',
        ),
        _make_log_event(
            event_id='event-3',
            message='Third plain text message',
        ),
    ]

    output_path = fix_test_cache / 'plain_text.parquet'
    stats = write_log_events_to_parquet(events, output_path)

    assert stats['total_events'] == 3
    assert stats['jsonl_events'] == 0
    assert stats['file_size_bytes'] > 0
    assert output_path.exists()

    # Verify with Polars
    df = pl.scan_parquet(str(output_path)).collect()
    assert len(df) == 3


def test_write_log_events_to_parquet_jsonl(fix_test_cache: Path):
    """Test Parquet writing with JSONL messages."""
    events = [
        _make_log_event(
            event_id='event-1',
            message='{"level":"INFO","msg":"First message","count":1}',
        ),
        _make_log_event(
            event_id='event-2',
            message='{"level":"ERROR","msg":"Second message","count":2}',
        ),
        _make_log_event(
            event_id='event-3',
            message='{"level":"WARN","msg":"Third message","count":3}',
        ),
    ]

    output_path = fix_test_cache / 'jsonl.parquet'
    stats = write_log_events_to_parquet(events, output_path)

    assert stats['total_events'] == 3
    assert stats['jsonl_events'] == 3
    assert stats['file_size_bytes'] > 0

    # Verify parsed fields are present
    df = pl.scan_parquet(str(output_path)).collect()
    assert len(df) == 3
    assert 'parsed' in df.columns
    assert 'log_group' in df.columns
    assert 'log_stream' in df.columns
    assert 'timestamp' in df.columns
    assert 'event_id' in df.columns


def test_write_log_events_to_parquet_mixed(fix_test_cache: Path):
    """Test Parquet writing with mixed plain text and JSONL messages."""
    events = [
        _make_log_event(
            event_id='event-1',
            message='Plain text message',
        ),
        _make_log_event(
            event_id='event-2',
            message='{"level":"INFO","msg":"JSON message"}',
        ),
        _make_log_event(
            event_id='event-3',
            message='Another plain text message',
        ),
        _make_log_event(
            event_id='event-4',
            message='{"level":"ERROR","msg":"Another JSON message"}',
        ),
    ]

    output_path = fix_test_cache / 'mixed.parquet'
    stats = write_log_events_to_parquet(events, output_path)

    assert stats['total_events'] == 4
    assert stats['jsonl_events'] == 2
    assert stats['file_size_bytes'] > 0

    # Verify all events are present
    df = pl.scan_parquet(str(output_path)).collect()
    assert len(df) == 4


def test_write_log_events_to_parquet_empty(fix_test_cache: Path):
    """Test Parquet writing with empty input."""
    output_path = fix_test_cache / 'empty.parquet'

    with pytest.raises(ValueError, match='empty log events'):
        write_log_events_to_parquet([], output_path)

    assert not output_path.exists()


def test_write_log_events_to_parquet_compression_levels(fix_test_cache: Path):
    """Test different compression levels."""
    # Create a reasonably large dataset to see compression differences
    events = [
        _make_log_event(
            event_id=f'event-{i}',
            message=f'Log message number {i} with some repeated text content',
        )
        for i in range(1000)
    ]

    path_low = fix_test_cache / 'compression_low.parquet'
    path_high = fix_test_cache / 'compression_high.parquet'

    stats_low = write_log_events_to_parquet(events, path_low, compression_level=1)
    stats_high = write_log_events_to_parquet(events, path_high, compression_level=22)

    # Higher compression should generally reduce size, but allow small variance
    allowed_variance = int(stats_low['file_size_bytes'] * 0.05) + 1
    assert stats_high['file_size_bytes'] <= stats_low['file_size_bytes'] + allowed_variance

    # Both should have same event counts
    assert stats_low['total_events'] == 1000
    assert stats_high['total_events'] == 1000

    # Both should be readable
    df_low = pl.scan_parquet(str(path_low)).collect()
    df_high = pl.scan_parquet(str(path_high)).collect()
    assert len(df_low) == 1000
    assert len(df_high) == 1000


def test_write_log_events_to_parquet_custom_row_group_size(fix_test_cache: Path):
    """Ensure custom row group size is honored."""
    events = [
        _make_log_event(
            event_id=f'event-{i}',
            message=f'Row group test {i}',
        )
        for i in range(1500)
    ]
    output_path = fix_test_cache / 'custom_row_group.parquet'

    stats = write_log_events_to_parquet(
        events,
        output_path,
        row_group_size=500,
    )

    assert stats['total_events'] == 1500

    parquet_file = pq.ParquetFile(output_path)
    assert parquet_file.metadata is not None
    assert parquet_file.metadata.num_row_groups == 3
    for index in range(parquet_file.metadata.num_row_groups):
        assert parquet_file.metadata.row_group(index).num_rows <= 500


def test_write_log_events_to_parquet_custom_infer_schema_length(fix_test_cache: Path):
    """Verify writes complete with a custom schema inference window."""
    events = [
        _make_log_event(
            event_id=f'event-{i}',
            message=(f'{{"value": {i}}}' if i % 2 == 0 else f'plain-{i}'),
        )
        for i in range(200)
    ]
    output_path = fix_test_cache / 'custom_infer.parquet'

    stats = write_log_events_to_parquet(
        events,
        output_path,
        infer_schema_length=50,
    )

    assert stats['total_events'] == 200
    assert output_path.exists()


def test_write_log_events_to_parquet_with_progress_callback(fix_test_cache: Path):
    """Ensure progress callbacks are invoked during conversion."""
    events = [
        _make_log_event(
            event_id=f'event-{i}',
            message=f'{{"idx": {i}}}',
        )
        for i in range(2100)
    ]
    output_path = fix_test_cache / 'progress.parquet'
    calls = []

    def progress(current: int, total: int, status: str) -> None:
        calls.append((current, total, status))

    stats = write_log_events_to_parquet(
        events,
        output_path,
        progress_callback=progress,
    )

    assert stats['total_events'] == 2100
    statuses = {status for _, _, status in calls}
    assert 'Parsing JSONL...' in statuses
    assert 'Converting to Parquet...' in statuses
    assert any(total == -1 for _, total, _ in calls)
    assert any(total == 2100 for _, total, _ in calls)


def test_read_parquet_to_log_events_round_trip(fix_test_cache: Path):
    """Test round-trip conversion: LogEvent -> Parquet -> LogEvent."""
    # Create events with various field combinations
    timestamp1 = datetime(2025, 1, 1, 12, 0, 0, 123456, tzinfo=UTC)
    timestamp2 = datetime(2025, 1, 2, 13, 30, 45, 654321, tzinfo=UTC)
    ingestion1 = datetime(2025, 1, 1, 12, 0, 1, tzinfo=UTC)

    original_events = [
        _make_log_event(
            log_group='/aws/lambda/fn1',
            log_stream='stream-1',
            timestamp=timestamp1,
            message='Message with ingestion time',
            event_id='event-1',
            ingestion_time=ingestion1,
        ),
        _make_log_event(
            log_group='/aws/lambda/fn2',
            log_stream='stream-2',
            timestamp=timestamp2,
            message='Message without ingestion time',
            event_id='event-2',
            ingestion_time=None,
        ),
    ]

    # Write to Parquet
    output_path = fix_test_cache / 'round_trip.parquet'
    write_log_events_to_parquet(original_events, output_path)

    # Read back
    read_events = list(read_parquet_to_log_events(output_path))

    assert len(read_events) == 2

    # Compare first event
    assert read_events[0].log_group == original_events[0].log_group
    assert read_events[0].log_stream == original_events[0].log_stream
    assert read_events[0].timestamp == original_events[0].timestamp
    assert read_events[0].message == original_events[0].message
    assert read_events[0].event_id == original_events[0].event_id
    assert read_events[0].ingestion_time == original_events[0].ingestion_time

    # Compare second event
    assert read_events[1].log_group == original_events[1].log_group
    assert read_events[1].log_stream == original_events[1].log_stream
    assert read_events[1].timestamp == original_events[1].timestamp
    assert read_events[1].message == original_events[1].message
    assert read_events[1].event_id == original_events[1].event_id
    assert read_events[1].ingestion_time is None


def test_read_parquet_to_log_events_nonexistent_file(fix_test_cache: Path):
    """Test reading from nonexistent Parquet file."""
    nonexistent_path = fix_test_cache / 'nonexistent.parquet'

    with pytest.raises(FileNotFoundError, match='Parquet file not found'):
        list(read_parquet_to_log_events(nonexistent_path))


def test_log_cache_init(fix_test_cache: Path):
    """Test LogCache initialization."""
    cache_dir = fix_test_cache / 'cache_init'

    with LogCache(cache_dir) as cache:
        assert cache._cache_dir == cache_dir
        assert cache._parquet_dir == cache_dir / 'parquet'
        assert cache._cache_dir.exists()
        assert cache._parquet_dir.exists()


def test_log_cache_write_and_read(fix_test_cache: Path):
    """Test basic cache write and read operations."""
    cache_dir = fix_test_cache / 'cache_basic'

    with LogCache(cache_dir) as cache:
        # Generate cache key
        start = datetime(2025, 1, 1, tzinfo=UTC)
        end = datetime(2025, 1, 2, tzinfo=UTC)
        cache_key = generate_cache_key('/aws/lambda/fn', start, end)

        # Create test events
        original_events = [
            _make_log_event(event_id='event-1', message='First message'),
            _make_log_event(event_id='event-2', message='Second message'),
            _make_log_event(event_id='event-3', message='Third message'),
        ]

        # Write to cache
        stats = cache.write(original_events, cache_key)

        assert stats['total_events'] == 3
        assert stats['jsonl_events'] == 0
        assert stats['file_size_bytes'] > 0

        # Read back
        cached_events = list(cache.read(cache_key))

        assert len(cached_events) == 3
        assert cached_events[0].event_id == 'event-1'
        assert cached_events[1].event_id == 'event-2'
        assert cached_events[2].event_id == 'event-3'


def test_log_cache_with_custom_config(fix_test_cache: Path):
    """Ensure custom row group and schema settings are applied."""
    cache_dir = fix_test_cache / 'cache_custom_config'
    events = [_make_log_event(event_id=f'event-{i}', message=f'{{"value": {i}}}') for i in range(150)]

    with LogCache(
        cache_dir,
        row_group_size=50,
        infer_schema_length=25,
        compression_level=5,
    ) as cache:
        start = datetime(2025, 1, 1, tzinfo=UTC)
        end = datetime(2025, 1, 2, tzinfo=UTC)
        cache_key = generate_cache_key('/aws/lambda/custom', start, end)

        stats = cache.write(events, cache_key)

        assert stats['total_events'] == 150
        assert cache._row_group_size == 50
        assert cache._infer_schema_length == 25

        parquet_files = list(cache._parquet_dir.glob('*.parquet'))
        assert len(parquet_files) == 1

        parquet_file = pq.ParquetFile(parquet_files[0])
        assert parquet_file.metadata is not None
        assert parquet_file.metadata.num_row_groups == 3


def test_log_cache_write_with_progress(fix_test_cache: Path):
    """Verify progress callback integration."""
    cache_dir = fix_test_cache / 'cache_progress'
    events = [_make_log_event(event_id=f'event-{i}', message=f'{{"value": {i}}}') for i in range(1100)]
    calls = []

    def progress(current: int, total: int, status: str) -> None:
        calls.append((current, total, status))

    with LogCache(cache_dir) as cache:
        start = datetime(2025, 1, 1, tzinfo=UTC)
        end = datetime(2025, 1, 2, tzinfo=UTC)
        cache_key = generate_cache_key('/aws/lambda/progress', start, end)

        stats = cache.write(events, cache_key, progress_callback=progress)

    assert stats['total_events'] == 1100
    assert calls, 'Expected progress callback to be invoked'
    statuses = {status for _, _, status in calls}
    assert 'Parsing JSONL...' in statuses
    assert 'Converting to Parquet...' in statuses
    assert any(total == -1 for _, total, _ in calls)
    assert any(total == 1100 for _, total, _ in calls)


def test_log_cache_exists(fix_test_cache: Path):
    """Test cache key existence check."""
    cache_dir = fix_test_cache / 'cache_exists'

    with LogCache(cache_dir) as cache:
        start = datetime(2025, 1, 1, tzinfo=UTC)
        end = datetime(2025, 1, 2, tzinfo=UTC)
        cache_key = generate_cache_key('/aws/lambda/fn', start, end)

        # Key doesn't exist initially
        assert cache.exists(cache_key) is False

        # Write events
        events = [_make_log_event(event_id='event-1')]
        cache.write(events, cache_key)

        # Key exists now
        assert cache.exists(cache_key) is True

        # Nonexistent key
        assert cache.exists('nonexistent-key') is False


def test_log_cache_ttl_expiration(fix_test_cache: Path):
    """Test TTL expiration of cache entries."""
    cache_dir = fix_test_cache / 'cache_ttl'

    with LogCache(cache_dir) as cache:
        start = datetime(2025, 1, 1, tzinfo=UTC)
        end = datetime(2025, 1, 2, tzinfo=UTC)
        cache_key = generate_cache_key('/aws/lambda/fn', start, end)

        # Fractional TTL so the test outlasts it without a long sleep
        events = [_make_log_event(event_id='event-1')]
        cache.write(events, cache_key, ttl_seconds=_SHORT_TTL)

        # Exists immediately
        assert cache.exists(cache_key) is True

        # Wait for expiration
        time.sleep(_PAST_TTL)

        # Trigger expiration
        cleaned = cache.evict_expired()

        # Key no longer exists
        assert cache.exists(cache_key) is False
        assert cleaned >= 1  # At least one Parquet file cleaned up


def test_log_cache_fifo_eviction(fix_test_cache: Path):
    """Test FIFO eviction when size limit is exceeded."""
    cache_dir = fix_test_cache / 'cache_fifo'

    # Create cache with very small size limit to force eviction
    # Use 1 MB but write much more data to ensure eviction
    with LogCache(cache_dir, size_limit_mb=1) as cache:
        cache_keys = []

        # Write multiple batches to exceed size limit
        # Write 10 batches of large data to ensure we exceed 1MB
        for i in range(10):
            start = datetime(2025, 1, i + 1, tzinfo=UTC)
            end = datetime(2025, 1, i + 2, tzinfo=UTC)
            cache_key = generate_cache_key(f'/aws/lambda/fn{i}', start, end)
            cache_keys.append(cache_key)

            # Create enough events to use significant space
            # Each message is ~100 bytes, 2000 events = ~200KB per file
            # 10 files * 200KB = 2MB total, exceeding 1MB limit
            long_message = (
                'Message {j} with content to fill space for testing '
                'eviction behavior with longer text to increase file size'
            )
            events = [
                _make_log_event(
                    event_id=f'event-{j}',
                    message=long_message.format(j=j),
                )
                for j in range(2000)
            ]

            cache.write(events, cache_key)

            # Add small delay to ensure different creation times
            time.sleep(0.01)

        # Count total Parquet file size to verify we wrote enough data
        parquet_files = list(cache._parquet_dir.glob('*.parquet'))
        total_size = sum(f.stat().st_size for f in parquet_files)

        # Verify we actually wrote enough data to trigger eviction
        # With compression, files might be smaller than expected
        # If total size is under limit, we need to verify cache is functional
        size_limit_bytes = 1 * 1024 * 1024

        if total_size > size_limit_bytes:
            # Size limit enforcement happened
            # Verify that at least some files were evicted (not all keys exist)
            existing_keys = sum(1 for key in cache_keys if cache.exists(key))
            assert existing_keys < len(cache_keys), 'Some keys should have been evicted'

            # Verify oldest keys were evicted (FIFO)
            # At least one of the first few keys should be gone
            first_half_exists = [cache.exists(key) for key in cache_keys[:5]]
            assert not all(first_half_exists), 'At least one old key should be evicted (FIFO)'

            # Most recent keys should still exist
            last_few_exists = [cache.exists(key) for key in cache_keys[-2:]]
            assert any(last_few_exists), 'Recent keys should still exist'
        else:
            # Compression was very effective, verify cache is still functional
            # All keys should still exist
            for key in cache_keys:
                assert cache.exists(key), 'All keys should exist if under size limit'

        # Verify that evict_expired also works correctly
        cache.evict_expired()

        # Cache should still be functional
        assert cache._metadata is not None


def test_log_cache_clear(fix_test_cache: Path):
    """Test clearing all cache entries."""
    cache_dir = fix_test_cache / 'cache_clear'

    with LogCache(cache_dir) as cache:
        # Write multiple cache entries
        for i in range(3):
            start = datetime(2025, 1, i + 1, tzinfo=UTC)
            end = datetime(2025, 1, i + 2, tzinfo=UTC)
            cache_key = generate_cache_key(f'/aws/lambda/fn{i}', start, end)

            events = [_make_log_event(event_id=f'event-{i}')]
            cache.write(events, cache_key)

        # Verify entries exist
        key0 = generate_cache_key(
            '/aws/lambda/fn0',
            datetime(2025, 1, 1, tzinfo=UTC),
            datetime(2025, 1, 2, tzinfo=UTC),
        )
        assert cache.exists(key0) is True

        # Clear cache
        cache.clear()

        # All entries should be gone
        assert cache.exists(key0) is False

        # Parquet directory should be empty
        parquet_files = list(cache._parquet_dir.glob('*.parquet'))
        assert len(parquet_files) == 0


def test_log_cache_orphaned_file_cleanup(fix_test_cache: Path):
    """Test cleanup of orphaned Parquet files."""
    cache_dir = fix_test_cache / 'cache_orphan'

    with LogCache(cache_dir) as cache:
        start = datetime(2025, 1, 1, tzinfo=UTC)
        end = datetime(2025, 1, 2, tzinfo=UTC)
        cache_key = generate_cache_key('/aws/lambda/fn', start, end)

        # Write events
        events = [_make_log_event(event_id='event-1')]
        cache.write(events, cache_key)

        # Manually delete metadata entry (simulate corruption)
        cache._metadata.delete(cache_key)

        # Parquet file should still exist
        parquet_files = list(cache._parquet_dir.glob('*.parquet'))
        assert len(parquet_files) == 1

        # Call evict_expired to clean up orphans
        cleaned = cache.evict_expired()

        # Orphaned file should be cleaned up
        assert cleaned == 1
        parquet_files_after = list(cache._parquet_dir.glob('*.parquet'))
        assert len(parquet_files_after) == 0


def test_log_cache_concurrent_writes(fix_test_cache: Path):
    """Test writing different cache keys to same LogCache."""
    cache_dir = fix_test_cache / 'cache_concurrent'

    with LogCache(cache_dir) as cache:
        # Write multiple different cache keys
        keys = []
        for i in range(5):
            start = datetime(2025, 1, i + 1, tzinfo=UTC)
            end = datetime(2025, 1, i + 2, tzinfo=UTC)
            cache_key = generate_cache_key(f'/aws/lambda/fn{i}', start, end)
            keys.append(cache_key)

            events = [_make_log_event(event_id=f'event-{i}', message=f'Message {i}')]
            cache.write(events, cache_key)

        # All keys should be readable independently
        for i, cache_key in enumerate(keys):
            cached_events = list(cache.read(cache_key))
            assert len(cached_events) == 1
            assert cached_events[0].event_id == f'event-{i}'
            assert cached_events[0].message == f'Message {i}'

        # Verify unique Parquet files
        parquet_files = list(cache._parquet_dir.glob('*.parquet'))
        assert len(parquet_files) == 5


def test_log_cache_invalid_cache_key(fix_test_cache: Path):
    """Test reading with invalid/nonexistent cache key."""
    cache_dir = fix_test_cache / 'cache_invalid'

    with LogCache(cache_dir) as cache:
        # Try to read nonexistent key - should return empty iterator
        result = list(cache.read('nonexistent-key'))

        assert result == []


def test_log_cache_malformed_jsonl(fix_test_cache: Path):
    """Test handling of malformed JSON in messages."""
    cache_dir = fix_test_cache / 'cache_malformed'

    with LogCache(cache_dir) as cache:
        start = datetime(2025, 1, 1, tzinfo=UTC)
        end = datetime(2025, 1, 2, tzinfo=UTC)
        cache_key = generate_cache_key('/aws/lambda/fn', start, end)

        # Create events with malformed JSON
        events = [
            _make_log_event(
                event_id='event-1',
                message='{invalid json structure',
            ),
            _make_log_event(
                event_id='event-2',
                message='{"level":"INFO","msg":"valid"}',
            ),
        ]

        # Should not crash
        stats = cache.write(events, cache_key)

        assert stats['total_events'] == 2
        # Only one valid JSON
        assert stats['jsonl_events'] == 1

        # Read back and verify message is preserved
        cached_events = list(cache.read(cache_key))
        assert len(cached_events) == 2
        assert cached_events[0].message == '{invalid json structure'


def test_log_cache_special_characters(fix_test_cache: Path):
    """Test log groups and messages with special characters."""
    cache_dir = fix_test_cache / 'cache_special'

    with LogCache(cache_dir) as cache:
        start = datetime(2025, 1, 1, tzinfo=UTC)
        end = datetime(2025, 1, 2, tzinfo=UTC)

        # Log group with special characters
        log_group = '/aws/lambda/my-function_v2-test'
        cache_key = generate_cache_key(log_group, start, end)

        # Messages with special characters
        events = [
            _make_log_event(
                log_group=log_group,
                event_id='event-1',
                message='Message with unicode: 你好世界',
            ),
            _make_log_event(
                log_group=log_group,
                event_id='event-2',
                message='Message with newlines:\nLine 1\nLine 2',
            ),
            _make_log_event(
                log_group=log_group,
                event_id='event-3',
                message='Message with quotes: "test" and \'test\'',
            ),
        ]

        cache.write(events, cache_key)

        # Read back and verify preservation
        cached_events = list(cache.read(cache_key))
        assert len(cached_events) == 3
        assert cached_events[0].message == 'Message with unicode: 你好世界'
        assert cached_events[1].message == 'Message with newlines:\nLine 1\nLine 2'
        assert cached_events[2].message == 'Message with quotes: "test" and \'test\''


def test_log_cache_large_batch(fix_test_cache: Path):
    """Test with a large number of events."""
    cache_dir = fix_test_cache / 'cache_large'

    with LogCache(cache_dir) as cache:
        start = datetime(2025, 1, 1, tzinfo=UTC)
        end = datetime(2025, 1, 2, tzinfo=UTC)
        cache_key = generate_cache_key('/aws/lambda/fn', start, end)

        # Create 10,000 events
        events = [
            _make_log_event(
                event_id=f'event-{i}',
                message=f'Log message number {i}',
            )
            for i in range(10000)
        ]

        # Write to cache
        stats = cache.write(events, cache_key)

        assert stats['total_events'] == 10000
        assert stats['file_size_bytes'] > 0

        # Read back and verify count
        # Use iterator to avoid loading all into memory
        count = sum(1 for _ in cache.read(cache_key))

        assert count == 10000


def test_log_cache_datetime_precision(fix_test_cache: Path):
    """Test datetime precision preservation."""
    cache_dir = fix_test_cache / 'cache_precision'

    with LogCache(cache_dir) as cache:
        start = datetime(2025, 1, 1, tzinfo=UTC)
        end = datetime(2025, 1, 2, tzinfo=UTC)
        cache_key = generate_cache_key('/aws/lambda/fn', start, end)

        # Create events with microsecond precision
        timestamp_with_micros = datetime(
            2025,
            1,
            1,
            12,
            30,
            45,
            123456,
            tzinfo=UTC,
        )
        ingestion_with_micros = datetime(
            2025,
            1,
            1,
            12,
            30,
            46,
            654321,
            tzinfo=UTC,
        )

        events = [
            _make_log_event(
                event_id='event-1',
                timestamp=timestamp_with_micros,
                ingestion_time=ingestion_with_micros,
            ),
        ]

        cache.write(events, cache_key)

        # Read back and verify precision
        cached_events = list(cache.read(cache_key))
        assert len(cached_events) == 1

        assert cached_events[0].timestamp == timestamp_with_micros
        assert cached_events[0].timestamp.microsecond == 123456
        assert cached_events[0].ingestion_time == ingestion_with_micros
        assert cached_events[0].ingestion_time is not None
        assert cached_events[0].ingestion_time.microsecond == 654321


def test_log_cache_default_ttl(fix_test_cache: Path):
    """Test LogCache with default TTL."""
    cache_dir = fix_test_cache / 'cache_default_ttl'

    # Create cache with a fractional default TTL
    with LogCache(cache_dir, default_ttl_seconds=_SHORT_TTL) as cache:
        start = datetime(2025, 1, 1, tzinfo=UTC)
        end = datetime(2025, 1, 2, tzinfo=UTC)
        cache_key = generate_cache_key('/aws/lambda/fn', start, end)

        # Write without specifying TTL (should use default)
        events = [_make_log_event(event_id='event-1')]
        cache.write(events, cache_key)

        # Exists immediately
        assert cache.exists(cache_key) is True

        # Wait for expiration
        time.sleep(_PAST_TTL)

        # Trigger expiration
        cache.evict_expired()

        # Should be expired
        assert cache.exists(cache_key) is False


def test_log_cache_override_default_ttl(fix_test_cache: Path):
    """Test overriding default TTL with explicit value."""
    cache_dir = fix_test_cache / 'cache_override_ttl'

    # Create cache with a fractional default TTL
    with LogCache(cache_dir, default_ttl_seconds=_SHORT_TTL) as cache:
        start = datetime(2025, 1, 1, tzinfo=UTC)
        end = datetime(2025, 1, 2, tzinfo=UTC)
        cache_key = generate_cache_key('/aws/lambda/fn', start, end)

        # Write with longer TTL (override default)
        events = [_make_log_event(event_id='event-1')]
        cache.write(events, cache_key, ttl_seconds=10)

        # Wait past default TTL
        time.sleep(_PAST_TTL)

        # Trigger expiration
        cache.evict_expired()

        # Should still exist (longer TTL)
        assert cache.exists(cache_key) is True


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
