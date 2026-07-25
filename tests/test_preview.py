"""Tests for log group previews."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tail_cw.aws.client import LogEvent
from tail_cw.cache.storage import LogCache, generate_preview_cache_key
from tail_cw.config.config import CacheConfig, PreviewConfig, TailCWConfig
from tail_cw.preview import DEFAULT_VOLUME_BUCKETS, bucket_event_counts, build_group_preview

_CLIENT = object()

NOW = datetime(2026, 1, 15, 12, tzinfo=UTC)
WINDOW = timedelta(minutes=15)
GROUP = '/aws/lambda/api-handler'


def _make_config(cache_dir: Path, *, ttl_seconds: int = 300) -> TailCWConfig:
    return TailCWConfig(
        cache=CacheConfig(cache_dir=cache_dir),
        preview=PreviewConfig(ttl_seconds=ttl_seconds),
    )


def _make_event(message: str, offset: timedelta) -> LogEvent:
    return LogEvent(
        log_group=GROUP,
        log_stream='2026/01/15/[$LATEST]abc',
        timestamp=NOW - offset,
        message=message,
        event_id=f'event-{message}-{offset.total_seconds()}',
        ingestion_time=None,
    )


class _RecordingFetcher:
    """Fake `fetch_log_events` that records every call it receives."""

    def __init__(self, messages: list[str]) -> None:
        self._messages = messages
        self.calls: list[dict[str, object]] = []

    async def __call__(
        self,
        _client: object,
        log_group_name: str,
        start_time: datetime,
        end_time: datetime,
        **kwargs: object,
    ) -> AsyncIterator[LogEvent]:
        self.calls.append(
            {
                'log_group_name': log_group_name,
                'start_time': start_time,
                'end_time': end_time,
                **kwargs,
            },
        )
        for index, message in enumerate(self._messages):
            yield _make_event(message, timedelta(seconds=index))


async def test_preview_clusters_sampled_messages(fix_test_cache: Path):
    fetcher = _RecordingFetcher(
        [
            'request completed status=200 duration=12ms',
            'request completed status=500 duration=98ms',
            'Timeout connecting to db:5432',
        ],
    )

    preview = await build_group_preview(
        _CLIENT,
        GROUP,
        window=WINDOW,
        now=NOW,
        config=_make_config(fix_test_cache),
        fetch_events=fetcher,
    )

    assert preview.log_group == GROUP
    assert preview.event_count == 3
    assert preview.window_seconds == 900
    assert [pattern.count for pattern in preview.patterns] == [2, 1]
    assert preview.patterns[0].key == 'request completed status=<n> duration=<n>ms'
    assert preview.patterns[0].example == 'request completed status=200 duration=12ms'
    assert fetcher.calls[0]['start_time'] == NOW - WINDOW
    assert fetcher.calls[0]['end_time'] == NOW


async def test_preview_patterns_are_count_descending(fix_test_cache: Path):
    fetcher = _RecordingFetcher(['rare event'] + ['common event'] * 3 + ['middling event'] * 2)

    preview = await build_group_preview(
        _CLIENT,
        GROUP,
        window=WINDOW,
        now=NOW,
        config=_make_config(fix_test_cache),
        fetch_events=fetcher,
    )

    counts = [pattern.count for pattern in preview.patterns]
    assert counts == sorted(counts, reverse=True)
    assert preview.patterns[0].example == 'common event'


async def test_preview_honors_sample_limit(fix_test_cache: Path):
    fetcher = _RecordingFetcher([f'message {index}' for index in range(50)])

    preview = await build_group_preview(
        _CLIENT,
        GROUP,
        window=WINDOW,
        now=NOW,
        config=_make_config(fix_test_cache),
        fetch_events=fetcher,
        sample_limit=7,
    )

    assert preview.event_count == 7


async def test_preview_caches_within_ttl(fix_test_cache: Path):
    fetcher = _RecordingFetcher(['request completed status=200'] * 4)
    config = _make_config(fix_test_cache)

    first = await build_group_preview(_CLIENT, GROUP, window=WINDOW, now=NOW, config=config, fetch_events=fetcher)
    second = await build_group_preview(
        _CLIENT,
        GROUP,
        window=WINDOW,
        now=NOW + timedelta(seconds=30),
        config=config,
        fetch_events=fetcher,
    )

    assert len(fetcher.calls) == 1
    assert second == first


async def test_preview_refetches_after_ttl_expiry(fix_test_cache: Path):
    fetcher = _RecordingFetcher(['request completed status=200'])
    config = _make_config(fix_test_cache, ttl_seconds=-1)

    await build_group_preview(_CLIENT, GROUP, window=WINDOW, now=NOW, config=config, fetch_events=fetcher)
    await build_group_preview(_CLIENT, GROUP, window=WINDOW, now=NOW, config=config, fetch_events=fetcher)

    assert len(fetcher.calls) == 2


async def test_preview_refetches_for_a_different_window(fix_test_cache: Path):
    fetcher = _RecordingFetcher(['request completed status=200'])
    config = _make_config(fix_test_cache)

    await build_group_preview(_CLIENT, GROUP, window=WINDOW, now=NOW, config=config, fetch_events=fetcher)
    await build_group_preview(_CLIENT, GROUP, window=timedelta(hours=1), now=NOW, config=config, fetch_events=fetcher)

    assert len(fetcher.calls) == 2


async def test_preview_refetches_for_a_different_profile(fix_test_cache: Path):
    fetcher = _RecordingFetcher(['request completed status=200'])
    config = _make_config(fix_test_cache)

    kwargs: dict[str, Any] = {'window': WINDOW, 'now': NOW, 'config': config, 'fetch_events': fetcher}
    await build_group_preview(_CLIENT, GROUP, profile_name='dev', **kwargs)
    await build_group_preview(_CLIENT, GROUP, profile_name='prod', **kwargs)

    # The profile reaches the cache key, not the fetch, so two calls prove the keys differ
    assert len(fetcher.calls) == 2


async def test_preview_empty_group(fix_test_cache: Path):
    fetcher = _RecordingFetcher([])

    preview = await build_group_preview(
        _CLIENT,
        GROUP,
        window=WINDOW,
        now=NOW,
        config=_make_config(fix_test_cache),
        fetch_events=fetcher,
    )

    assert preview.event_count == 0
    assert preview.patterns == []


async def test_preview_ignores_an_unrecognized_cached_payload(fix_test_cache: Path):
    fetcher = _RecordingFetcher(['request completed status=200'])
    config = _make_config(fix_test_cache)
    cache_key = generate_preview_cache_key(GROUP, window_seconds=900)
    with LogCache(fix_test_cache) as cache:
        cache.write_payload(cache_key, {'unexpected': 'shape'})

    preview = await build_group_preview(_CLIENT, GROUP, window=WINDOW, now=NOW, config=config, fetch_events=fetcher)

    assert preview.event_count == 1
    assert len(fetcher.calls) == 1


async def test_preview_defaults_to_the_aws_fetcher(monkeypatch: pytest.MonkeyPatch, fix_test_cache: Path):
    fetcher = _RecordingFetcher(['request completed status=200'])
    monkeypatch.setattr('tail_cw.preview.fetch_log_events', fetcher)

    preview = await build_group_preview(_CLIENT, GROUP, window=WINDOW, now=NOW, config=_make_config(fix_test_cache))

    assert preview.event_count == 1
    assert len(fetcher.calls) == 1


async def test_preview_falls_back_to_the_default_cache_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    default_dir = tmp_path / 'default_cache'
    monkeypatch.setattr('tail_cw.preview.get_default_cache_dir', lambda: default_dir)
    fetcher = _RecordingFetcher(['request completed status=200'])

    preview = await build_group_preview(
        _CLIENT,
        GROUP,
        window=WINDOW,
        now=NOW,
        config=TailCWConfig(),
        fetch_events=fetcher,
    )

    assert preview.event_count == 1
    assert default_dir.exists()


def test_preview_cache_key_varies_by_inputs():
    base = generate_preview_cache_key(GROUP, window_seconds=900)

    assert base == generate_preview_cache_key(GROUP, window_seconds=900)
    assert base.startswith('preview:v1:')
    assert base != generate_preview_cache_key(GROUP, window_seconds=3600)
    assert base != generate_preview_cache_key('/aws/lambda/other', window_seconds=900)
    assert base != generate_preview_cache_key(GROUP, window_seconds=900, profile_name='prod')
    assert base != generate_preview_cache_key(GROUP, window_seconds=900, region_name='us-west-2')


def _at(minutes: float) -> datetime:
    return NOW - WINDOW + timedelta(minutes=minutes)


def test_bucket_event_counts_spreads_events_across_the_window():
    """Each timestamp lands in the bucket covering its offset."""
    counts = bucket_event_counts(
        [_at(0), _at(1), _at(7.5), _at(14)],
        start=NOW - WINDOW,
        end=NOW,
        buckets=3,
    )

    assert counts == [2.0, 1.0, 1.0]


def test_bucket_event_counts_puts_the_end_in_the_final_bucket():
    """A timestamp exactly at the window end counts rather than falling off."""
    counts = bucket_event_counts([NOW], start=NOW - WINDOW, end=NOW, buckets=4)

    assert counts == [0.0, 0.0, 0.0, 1.0]


def test_bucket_event_counts_ignores_events_outside_the_window():
    """Events before the start or after the end are dropped."""
    counts = bucket_event_counts(
        [_at(-5), _at(5), NOW + timedelta(minutes=1)],
        start=NOW - WINDOW,
        end=NOW,
        buckets=3,
    )

    assert counts == [0.0, 1.0, 0.0]


def test_bucket_event_counts_with_no_events_is_all_zero():
    """An empty group still renders a flat series rather than nothing."""
    assert bucket_event_counts([], start=NOW - WINDOW, end=NOW, buckets=3) == [0.0, 0.0, 0.0]


@pytest.mark.parametrize(
    ('start', 'end', 'buckets'),
    [
        (NOW, NOW, 4),
        (NOW, NOW - WINDOW, 4),
        (NOW - WINDOW, NOW, 0),
        (NOW - WINDOW, NOW, -1),
    ],
)
def test_bucket_event_counts_refuses_a_degenerate_request(start: datetime, end: datetime, buckets: int):
    """An empty window or a non-positive bucket count yields nothing to render."""
    assert bucket_event_counts([NOW], start=start, end=end, buckets=buckets) == []


def test_bucket_event_counts_defaults_to_the_module_bucket_count():
    """The default resolution is high enough for the sparkline to resample down."""
    counts = bucket_event_counts([_at(1)], start=NOW - WINDOW, end=NOW)

    assert len(counts) == DEFAULT_VOLUME_BUCKETS
    assert sum(counts) == pytest.approx(1.0)
