"""AWS CloudWatch Logs client module.

Provides streaming access to CloudWatch Logs via aiobotocore with efficient
pagination. Clients are owned by a :class:`ClientPool` held open for the life of
a session rather than built per call, so credentials resolve once and the
connection pool is reused. Every function here takes an already-open client,
which keeps credential resolution at the edges and leaves the request-shaping
logic straightforward to test with a fake.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, runtime_checkable

from aiobotocore.session import AioSession  # type: ignore[import-untyped]
from beartype.typing import Protocol
from botocore.config import Config  # type: ignore[import-untyped]

ProgressCallback = Callable[[int, str], None]

RETRIES: dict[str, object] = {'max_attempts': 10, 'mode': 'standard'}
"""Standard-mode retries, which back off on throttling without a custom handler."""


def retry_config() -> Config:
    """Build the client retry config.

    Returns a new instance per call because botocore rewrites the ``retries`` dict
    of whatever Config it is handed (``max_attempts`` becomes ``total_max_attempts``),
    so one shared instance would leak that rewrite between clients.
    """
    return Config(retries=dict(RETRIES))


@dataclass(frozen=True)
class LogEvent:
    """Represents a single CloudWatch Logs event.

    Attributes:
        log_group: The CloudWatch log group name.
        log_stream: The CloudWatch log stream name.
        timestamp: Event timestamp as timezone-aware datetime (UTC). Converted from
            epoch milliseconds returned by CloudWatch API.
        message: The log message content.
        event_id: Unique event identifier for deduplication.
        ingestion_time: When CloudWatch ingested the event. May be None if not
            provided in the API response.
    """

    log_group: str
    log_stream: str
    timestamp: datetime
    message: str
    event_id: str
    ingestion_time: datetime | None


def _epoch_ms_to_datetime(epoch_ms: int) -> datetime:
    """Convert epoch milliseconds to timezone-aware datetime in UTC.

    Args:
        epoch_ms: Timestamp in milliseconds since Unix epoch.

    Returns:
        Timezone-aware datetime object in UTC.
    """
    return datetime.fromtimestamp(epoch_ms / 1000.0, tz=UTC)


@runtime_checkable
class ClientProvider(Protocol):
    """Source of open AWS clients, keyed by service name.

    What the service layer depends on, so a caller can supply a fake without
    standing up credentials.
    """

    async def client(self, service_name: str) -> Any:
        """Return an open client for a service."""
        raise NotImplementedError


@dataclass
class ClientPool:
    """Holds one open client per service for the life of a session.

    Do not use after the surrounding :func:`client_pool` block exits; every
    client it handed out is closed on the way out.
    """

    session: AioSession
    stack: AsyncExitStack
    region_name: str | None = None
    _clients: dict[str, Any] = field(default_factory=dict)

    async def client(self, service_name: str) -> Any:
        """Return the open client for a service, creating it on first request."""
        if (existing := self._clients.get(service_name)) is not None:
            return existing
        client = await self.stack.enter_async_context(
            self.session.create_client(service_name, config=retry_config(), region_name=self.region_name),
        )
        self._clients[service_name] = client
        return client


@asynccontextmanager
async def client_pool(
    *,
    profile_name: str | None = None,
    region_name: str | None = None,
) -> AsyncIterator[ClientPool]:
    """Open a pool of AWS clients sharing one credential resolution.

    Resolves credentials through a named profile when given, otherwise the
    default credential chain (environment, shared config, SSO, IAM roles).

    Yields:
        A pool whose clients stay open until the block exits.
    """
    async with AsyncExitStack() as stack:
        yield ClientPool(session=AioSession(profile=profile_name), stack=stack, region_name=region_name)


async def fetch_log_events(
    client: Any,
    log_group_name: str,
    start_time: datetime,
    end_time: datetime,
    *,
    filter_pattern: str | None = None,
    log_stream_names: list[str] | None = None,
    interleaved: bool = True,
    progress_callback: ProgressCallback | None = None,
) -> AsyncIterator[LogEvent]:
    """Fetch CloudWatch Logs events using efficient pagination.

    Streams log events from CloudWatch Logs without loading all events into memory.
    Uses the paginator to handle pagination automatically with retry logic for
    throttling errors.

    Args:
        client: An open CloudWatch Logs client, from :meth:`ClientPool.client`.
        log_group_name: CloudWatch log group name to query.
        start_time: Start of time range (inclusive). Must be timezone-aware.
        end_time: End of time range (inclusive). Must be timezone-aware.
        filter_pattern: Optional CloudWatch Logs filter pattern for server-side
            filtering. See AWS documentation for filter syntax.
        log_stream_names: Optional list of log stream names to filter. If None,
            searches all streams in the log group.
        interleaved: Whether to interleave events from multiple streams
            chronologically. Default is True.
        progress_callback: Optional callable invoked every 100 events fetched.

    Yields:
        LogEvent instances for each log event in the time range.

    Example:
        >>> async with client_pool(region_name='us-east-1') as pool:
        ...     logs = await pool.client('logs')
        ...     async for event in fetch_log_events(logs, '/aws/lambda/f', start, end):
        ...         print(f'{event.timestamp}: {event.message}')

    Note:
        Errors from botocore (e.g., access denied, invalid log group) bubble up to
        the caller. Throttling errors are handled automatically via the retry
        configuration on the pool's clients.
    """
    kwargs: dict[str, Any] = {
        'logGroupName': log_group_name,
        'startTime': int(start_time.timestamp() * 1000),
        'endTime': int(end_time.timestamp() * 1000),
        'interleaved': interleaved,
    }
    if filter_pattern is not None:
        kwargs['filterPattern'] = filter_pattern
    if log_stream_names is not None:
        kwargs['logStreamNames'] = log_stream_names

    paginator = client.get_paginator('filter_log_events')
    page_iterator = paginator.paginate(PaginationConfig={'PageSize': 10000}, **kwargs)

    event_count = 0
    async for page in page_iterator:
        for event in page.get('events', []):
            event_count += 1
            if progress_callback and event_count % 100 == 0:
                progress_callback(event_count, f'Fetched {event_count} events...')

            ingestion_time = _epoch_ms_to_datetime(event['ingestionTime']) if 'ingestionTime' in event else None
            yield LogEvent(
                log_group=log_group_name,
                log_stream=event['logStreamName'],
                timestamp=_epoch_ms_to_datetime(event['timestamp']),
                message=event['message'],
                event_id=event['eventId'],
                ingestion_time=ingestion_time,
            )
