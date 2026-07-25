"""Live tail streaming over the CloudWatch Logs StartLiveTail API.

Wraps the event stream returned by ``StartLiveTail`` in an async generator of
:class:`LogEvent` instances. Handles log group name to ARN resolution, session
expiry (sessions end after roughly three hours), and transient stream errors
with bounded automatic reconnects. This module has no Textual dependency.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Callable, Sequence
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError  # type: ignore[import-untyped]

from tail_cw.aws.client import LogEvent, _epoch_ms_to_datetime

MAX_LIVE_TAIL_LOG_GROUPS = 10
"""StartLiveTail accepts at most 10 logGroupIdentifiers."""

SampledCallback = Callable[[bool], None]


class LiveTailSessionError(Exception):
    """Raised when a live tail session cannot be re-established after bounded retries."""


def _log_group_name_from_identifier(identifier: str) -> str:
    if identifier.startswith('arn:') and ':log-group:' in identifier:
        return identifier.split(':log-group:', maxsplit=1)[1].removesuffix(':*')
    return identifier


def _synthesize_event_id(log_stream: str, timestamp_ms: int, message: str) -> str:
    digest = hashlib.sha256(f'{log_stream}\x00{timestamp_ms}\x00{message}'.encode()).hexdigest()
    return f'live-{timestamp_ms}-{digest[:16]}'


def _live_event_to_log_event(raw: dict[str, Any]) -> LogEvent:
    """Convert a LiveTailSessionLogEvent dict to a LogEvent.

    Live tail events carry no ``eventId``, so a deterministic identifier is
    synthesized from the stream, timestamp, and message.
    """
    timestamp_ms = raw['timestamp']
    log_stream = raw.get('logStreamName', '')
    message = raw.get('message', '')
    ingestion_ms = raw.get('ingestionTime')
    return LogEvent(
        log_group=_log_group_name_from_identifier(raw.get('logGroupIdentifier', '')),
        log_stream=log_stream,
        timestamp=_epoch_ms_to_datetime(timestamp_ms),
        message=message,
        event_id=_synthesize_event_id(log_stream, timestamp_ms, message),
        ingestion_time=_epoch_ms_to_datetime(ingestion_ms) if ingestion_ms is not None else None,
    )


async def resolve_log_group_arns(client: Any, log_group_names: Sequence[str]) -> list[str]:
    """Resolve log group names to ARNs accepted by StartLiveTail.

    ``DescribeLogGroups`` returns ``arn`` values with a trailing ``:*`` that
    ``logGroupIdentifiers`` rejects; the unsuffixed ``logGroupArn`` field is
    preferred and the suffix is stripped as a fallback.

    Raises:
        ValueError: If any requested log group does not exist.
    """
    arns: dict[str, str] = {}
    paginator = client.get_paginator('describe_log_groups')
    for name in log_group_names:
        if name in arns:
            continue
        async for page in paginator.paginate(logGroupNamePrefix=name):
            for group in page.get('logGroups', []):
                group_name = group.get('logGroupName')
                if group_name in arns or group_name not in log_group_names:
                    continue
                arn = group.get('logGroupArn') or group['arn'].removesuffix(':*')
                arns[group_name] = arn
        if name not in arns:
            msg = f'Log group not found: {name}'
            raise ValueError(msg)
    return [arns[name] for name in log_group_names]


def _build_start_kwargs(
    identifiers: list[str],
    filter_pattern: str | None,
    log_stream_names: list[str] | None,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {'logGroupIdentifiers': identifiers}
    if filter_pattern is not None:
        kwargs['logEventFilterPattern'] = filter_pattern
    if log_stream_names is not None:
        kwargs['logStreamNames'] = log_stream_names
    return kwargs


async def _iter_session_events(
    response_stream: Any,
    on_sampled: SampledCallback | None,
) -> AsyncIterator[LogEvent]:
    async for chunk in response_stream:
        update = chunk.get('sessionUpdate')
        if update is None:
            continue
        if on_sampled is not None and (metadata := update.get('sessionMetadata')) is not None:
            on_sampled(bool(metadata.get('sampled', False)))
        for raw in update.get('sessionResults', []):
            yield _live_event_to_log_event(raw)


async def stream_live_tail(
    client: Any,
    log_group_names: Sequence[str],
    *,
    filter_pattern: str | None = None,
    log_stream_names: list[str] | None = None,
    max_reconnect_attempts: int = 3,
    on_sampled: SampledCallback | None = None,
) -> AsyncIterator[LogEvent]:
    """Stream live log events for one or more log groups.

    Reconnects automatically when the session expires (~3h) or the stream
    fails transiently; consecutive failures beyond ``max_reconnect_attempts``
    raise :class:`LiveTailSessionError`. ``on_sampled`` is invoked with the
    ``sessionMetadata.sampled`` flag whenever the server reports it (true when
    more than 500 events/sec match and the stream is downsampled).

    Args:
        client: An open CloudWatch Logs client, from :meth:`ClientPool.client`.
        log_group_names: Between 1 and 10 log group names to tail.
        filter_pattern: Server-side ``logEventFilterPattern``, applied by AWS.
        log_stream_names: Restrict the session to these streams.
        max_reconnect_attempts: Consecutive failures tolerated before giving up.
        on_sampled: Called with the server's downsampling flag when reported.

    Yields:
        LogEvent instances as they arrive on the live tail stream.

    Raises:
        ValueError: If the number of log groups is not between 1 and 10, or a
            log group does not exist.
        LiveTailSessionError: When reconnect attempts are exhausted.
    """
    if not 1 <= len(log_group_names) <= MAX_LIVE_TAIL_LOG_GROUPS:
        msg = f'StartLiveTail supports 1 to {MAX_LIVE_TAIL_LOG_GROUPS} log groups, got {len(log_group_names)}'
        raise ValueError(msg)

    identifiers = await resolve_log_group_arns(client, log_group_names)
    start_kwargs = _build_start_kwargs(identifiers, filter_pattern, log_stream_names)

    consecutive_failures = 0
    while True:
        try:
            response = await client.start_live_tail(**start_kwargs)
            async for event in _iter_session_events(response['responseStream'], on_sampled):
                consecutive_failures = 0
                yield event
        except (BotoCoreError, ClientError) as err:
            consecutive_failures += 1
            if consecutive_failures > max_reconnect_attempts:
                msg = f'Live tail session failed after {max_reconnect_attempts} reconnect attempts: {err}'
                raise LiveTailSessionError(msg) from err
        else:
            return
