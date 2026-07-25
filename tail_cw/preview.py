"""Sample a log group's recent events and summarize their message shapes.

Answers "what is in this group?" cheaply enough to walk a long list of groups: one
capped fetch per group, clustered into recurring shapes, cached with a short TTL so
revisiting a group costs nothing. Core layer, so no Textual import belongs here.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import islice
from typing import Any

from tail_cw.aws.client import LogEvent, fetch_log_events
from tail_cw.cache.storage import LogCache, generate_preview_cache_key
from tail_cw.config.config import TailCWConfig, get_default_cache_dir
from tail_cw.query.patterns import MessagePattern, cluster_messages

FetchEvents = Callable[..., Iterator[LogEvent]]


@dataclass(frozen=True)
class GroupPreview:
    """Recent-event summary for one log group."""

    log_group: str
    event_count: int
    window_seconds: int
    patterns: list[MessagePattern]


def build_group_preview(
    log_group: str,
    *,
    window: timedelta,
    now: datetime,
    config: TailCWConfig,
    profile_name: str | None = None,
    region_name: str | None = None,
    fetch_events: FetchEvents | None = None,
    sample_limit: int = 500,
) -> GroupPreview:
    """Summarize the recent message shapes in a log group.

    Reads at most `sample_limit` events from the window ending at `now`, so a busy
    group costs one small call rather than the whole window. Results cache under
    `preview:v1:<digest>` for `config.preview.ttl_seconds`; a cache hit performs no
    AWS call at all.

    Args:
        log_group: CloudWatch log group name.
        window: How far back from `now` to sample.
        now: Timezone-aware end of the sampled window.
        config: Application configuration supplying cache and preview settings.
        profile_name: Optional AWS profile name.
        region_name: Optional AWS region name.
        fetch_events: Optional replacement for :func:`fetch_log_events`.
        sample_limit: Maximum number of events read from the window.

    Returns:
        A :class:`GroupPreview`; an empty group yields ``event_count=0`` and no patterns.
    """
    window_seconds = int(window.total_seconds())
    cache_key = generate_preview_cache_key(
        log_group,
        window_seconds=window_seconds,
        region_name=region_name,
        profile_name=profile_name,
    )
    cache_dir = config.cache.cache_dir if config.cache.cache_dir is not None else get_default_cache_dir()

    with LogCache(
        cache_dir,
        size_limit_mb=config.cache.size_limit_mb,
        default_ttl_seconds=config.cache.default_ttl_seconds,
        eviction_policy=config.cache.eviction_policy,
    ) as cache:
        payload = cache.read_payload(cache_key)
        if payload is not None and (cached := _decode_payload(log_group, payload)) is not None:
            return cached

        preview = _sample_group(
            log_group,
            window=window,
            window_seconds=window_seconds,
            now=now,
            profile_name=profile_name,
            region_name=region_name,
            fetch_events=fetch_events,
            sample_limit=sample_limit,
        )
        cache.write_payload(cache_key, _encode_payload(preview), ttl_seconds=config.preview.ttl_seconds)

    return preview


def _sample_group(
    log_group: str,
    *,
    window: timedelta,
    window_seconds: int,
    now: datetime,
    profile_name: str | None,
    region_name: str | None,
    fetch_events: FetchEvents | None,
    sample_limit: int,
) -> GroupPreview:
    effective_fetch = fetch_events if fetch_events is not None else fetch_log_events
    events = effective_fetch(
        log_group,
        now - window,
        now,
        profile_name=profile_name,
        region_name=region_name,
    )
    messages = [event.message for event in islice(iter(events), sample_limit)]

    return GroupPreview(
        log_group=log_group,
        event_count=len(messages),
        window_seconds=window_seconds,
        patterns=cluster_messages(messages),
    )


def _encode_payload(preview: GroupPreview) -> dict[str, Any]:
    return {
        'event_count': preview.event_count,
        'window_seconds': preview.window_seconds,
        'patterns': [
            {'key': pattern.key, 'count': pattern.count, 'example': pattern.example} for pattern in preview.patterns
        ],
    }


def _decode_payload(log_group: str, payload: dict[str, Any]) -> GroupPreview | None:
    event_count = payload.get('event_count')
    window_seconds = payload.get('window_seconds')
    patterns = payload.get('patterns')
    if not isinstance(event_count, int) or not isinstance(window_seconds, int) or not isinstance(patterns, list):
        return None

    return GroupPreview(
        log_group=log_group,
        event_count=event_count,
        window_seconds=window_seconds,
        patterns=[
            MessagePattern(key=pattern['key'], count=pattern['count'], example=pattern['example'])
            for pattern in patterns
        ],
    )
