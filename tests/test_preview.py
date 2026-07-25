"""Tests for log group previews."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tail_cw.aws.client import LogEvent
from tail_cw.cache.storage import LogCache, generate_preview_cache_key
from tail_cw.config.config import CacheConfig, PreviewConfig, TailCWConfig
from tail_cw.preview import build_group_preview

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

    def __call__(
        self,
        log_group_name: str,
        start_time: datetime,
        end_time: datetime,
        **kwargs: object,
    ) -> Iterator[LogEvent]:
        self.calls.append(
            {
                'log_group_name': log_group_name,
                'start_time': start_time,
                'end_time': end_time,
                **kwargs,
            },
        )
        return iter(
            [_make_event(message, timedelta(seconds=index)) for index, message in enumerate(self._messages)],
        )


def test_preview_clusters_sampled_messages(fix_test_cache: Path):
    fetcher = _RecordingFetcher(
        [
            'request completed status=200 duration=12ms',
            'request completed status=500 duration=98ms',
            'Timeout connecting to db:5432',
        ],
    )

    preview = build_group_preview(
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


def test_preview_patterns_are_count_descending(fix_test_cache: Path):
    fetcher = _RecordingFetcher(['rare event'] + ['common event'] * 3 + ['middling event'] * 2)

    preview = build_group_preview(
        GROUP,
        window=WINDOW,
        now=NOW,
        config=_make_config(fix_test_cache),
        fetch_events=fetcher,
    )

    counts = [pattern.count for pattern in preview.patterns]
    assert counts == sorted(counts, reverse=True)
    assert preview.patterns[0].example == 'common event'


def test_preview_honors_sample_limit(fix_test_cache: Path):
    fetcher = _RecordingFetcher([f'message {index}' for index in range(50)])

    preview = build_group_preview(
        GROUP,
        window=WINDOW,
        now=NOW,
        config=_make_config(fix_test_cache),
        fetch_events=fetcher,
        sample_limit=7,
    )

    assert preview.event_count == 7


def test_preview_caches_within_ttl(fix_test_cache: Path):
    fetcher = _RecordingFetcher(['request completed status=200'] * 4)
    config = _make_config(fix_test_cache)

    first = build_group_preview(GROUP, window=WINDOW, now=NOW, config=config, fetch_events=fetcher)
    second = build_group_preview(
        GROUP,
        window=WINDOW,
        now=NOW + timedelta(seconds=30),
        config=config,
        fetch_events=fetcher,
    )

    assert len(fetcher.calls) == 1
    assert second == first


def test_preview_refetches_after_ttl_expiry(fix_test_cache: Path):
    fetcher = _RecordingFetcher(['request completed status=200'])
    config = _make_config(fix_test_cache, ttl_seconds=-1)

    build_group_preview(GROUP, window=WINDOW, now=NOW, config=config, fetch_events=fetcher)
    build_group_preview(GROUP, window=WINDOW, now=NOW, config=config, fetch_events=fetcher)

    assert len(fetcher.calls) == 2


def test_preview_refetches_for_a_different_window(fix_test_cache: Path):
    fetcher = _RecordingFetcher(['request completed status=200'])
    config = _make_config(fix_test_cache)

    build_group_preview(GROUP, window=WINDOW, now=NOW, config=config, fetch_events=fetcher)
    build_group_preview(GROUP, window=timedelta(hours=1), now=NOW, config=config, fetch_events=fetcher)

    assert len(fetcher.calls) == 2


def test_preview_refetches_for_a_different_profile(fix_test_cache: Path):
    fetcher = _RecordingFetcher(['request completed status=200'])
    config = _make_config(fix_test_cache)

    build_group_preview(GROUP, window=WINDOW, now=NOW, config=config, fetch_events=fetcher, profile_name='dev')
    build_group_preview(GROUP, window=WINDOW, now=NOW, config=config, fetch_events=fetcher, profile_name='prod')

    assert len(fetcher.calls) == 2
    assert fetcher.calls[0]['profile_name'] == 'dev'
    assert fetcher.calls[1]['profile_name'] == 'prod'


def test_preview_empty_group(fix_test_cache: Path):
    fetcher = _RecordingFetcher([])

    preview = build_group_preview(
        GROUP,
        window=WINDOW,
        now=NOW,
        config=_make_config(fix_test_cache),
        fetch_events=fetcher,
    )

    assert preview.event_count == 0
    assert preview.patterns == []


def test_preview_ignores_an_unrecognized_cached_payload(fix_test_cache: Path):
    fetcher = _RecordingFetcher(['request completed status=200'])
    config = _make_config(fix_test_cache)
    cache_key = generate_preview_cache_key(GROUP, window_seconds=900)
    with LogCache(fix_test_cache) as cache:
        cache.write_payload(cache_key, {'unexpected': 'shape'})

    preview = build_group_preview(GROUP, window=WINDOW, now=NOW, config=config, fetch_events=fetcher)

    assert preview.event_count == 1
    assert len(fetcher.calls) == 1


def test_preview_defaults_to_the_aws_fetcher(monkeypatch: pytest.MonkeyPatch, fix_test_cache: Path):
    fetcher = _RecordingFetcher(['request completed status=200'])
    monkeypatch.setattr('tail_cw.preview.fetch_log_events', fetcher)

    preview = build_group_preview(GROUP, window=WINDOW, now=NOW, config=_make_config(fix_test_cache))

    assert preview.event_count == 1
    assert len(fetcher.calls) == 1


def test_preview_falls_back_to_the_default_cache_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    default_dir = tmp_path / 'default_cache'
    monkeypatch.setattr('tail_cw.preview.get_default_cache_dir', lambda: default_dir)
    fetcher = _RecordingFetcher(['request completed status=200'])

    preview = build_group_preview(
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
