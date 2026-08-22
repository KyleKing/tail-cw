"""Constructors shared across the suite, so a schema change lands in one place."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from tail_cw.aws.events import LogEvent

BASE_TIME = datetime(2025, 1, 15, 10, 0, tzinfo=UTC)
"""Fixed instant every factory counts from, so tests never depend on the clock."""

DEFAULT_GROUP = '/aws/test/group'


def make_event(
    message: str = 'Test message',
    *,
    log_group: str = DEFAULT_GROUP,
    log_stream: str = 'stream-0',
    timestamp: datetime | None = None,
    ingestion_offset: timedelta | None = timedelta(seconds=1),
) -> LogEvent:
    """Build one LogEvent.

    Args:
        message: Event body.
        log_group: Owning log group.
        log_stream: Owning log stream.
        timestamp: Event time, defaulting to :data:`BASE_TIME`.
        ingestion_offset: How long after the event CloudWatch ingested it; None
            leaves ``ingestion_time`` unset.
    """
    moment = BASE_TIME if timestamp is None else timestamp
    return LogEvent(
        log_group=log_group,
        log_stream=log_stream,
        timestamp=moment,
        message=message,
        ingestion_time=None if ingestion_offset is None else moment + ingestion_offset,
    )


def make_events(
    messages: Iterable[str], *, log_group: str = DEFAULT_GROUP, step_seconds: float = 1.0
) -> list[LogEvent]:
    """Build events one ``step_seconds`` apart, so their order is unambiguous."""
    return [
        make_event(message, log_group=log_group, timestamp=BASE_TIME + timedelta(seconds=index * step_seconds))
        for index, message in enumerate(messages)
    ]
