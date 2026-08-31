"""Run CloudWatch Logs Insights queries.

Insights answers aggregation questions server-side ("count these by day for a week") that
``FilterLogEvents`` can only answer by downloading every matching event first. It is not the
default path anywhere in tail-cw because it bills per gigabyte scanned while
``FilterLogEvents`` does not, so every result carries the scanned volume for the caller to
report.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from tail_cw.aws.log_groups import LogGroupInfo

DEFAULT_LIMIT = 1000
DEFAULT_POLL_SECONDS = 0.5
MAX_INSIGHTS_LOG_GROUPS = 50
MAX_INSIGHTS_WINDOW = timedelta(days=7)
"""Widest window a query may cover.

Insights bills on the bytes it reads inside the window, so the window is the one
input that decides the bill before the query runs.
"""

DOLLARS_PER_GB = 0.005
"""What Insights bills per gigabyte scanned in us-east-1 as of mid-2026."""

ASSUMED_RETENTION_DAYS = 30
"""Span assumed for a group that neither expires nor reports when it was created."""

SAMPLE_SLICES = 3
"""Slices sampled across the query's window to measure a group's throughput.

One slice is not enough. A production group measured between 5,773 and 14,900
bytes a second inside one hour, so a single sample of its first minute read the
hour 1.68x high, where three slices spread across it read 1.03x.
"""

SAMPLE_SLICE = timedelta(minutes=5)
"""Longest one slice may cover. A busy group truncates well inside this."""

SAMPLE_LIMIT = 10_000
"""Events read per sample. FilterLogEvents caps its own response at 1 MB regardless."""

SAMPLE_CONCURRENCY = 4
"""Samples in flight at once.

Nothing here touches the blocking pool, so this is not tied to its width. Raising
it to 8 was tried against an 18-group account and the wall clock swung between
4.6s and 13.6s at either setting, so there is no evidence for the wider fan-out.
"""

EVENT_OVERHEAD_BYTES = 26
"""What CloudWatch counts per event on top of the message, for the timestamp and metadata."""

_MIN_MEASURABLE_SPAN_SECONDS = 1.0

_BYTES_PER_GB = 1_000_000_000
_TERMINAL_STATUSES = frozenset({'Complete', 'Failed', 'Cancelled', 'Timeout', 'Unknown'})
_NARROWING_COMMANDS = {
    'CWLI': ('filter', 'pattern', 'dedup'),
    'PPL': ('where', 'stats', 'dedup', 'patterns'),
    'SQL': ('where', 'group by', 'having'),
}
"""What counts as narrowing, per language. Each says the same thing its own way."""

_NARROWING_PATTERNS = {
    language: re.compile(
        r'(?<![\w@])(' + '|'.join(command.replace(' ', r'\s+') for command in commands) + r')(?![\w@])',
        re.IGNORECASE,
    )
    for language, commands in _NARROWING_COMMANDS.items()
}


def validate_insights_request(
    query: str,
    start_time: datetime,
    end_time: datetime,
    language: str = 'CWLI',
) -> None:
    """Check a query before it is allowed to bill.

    Two guards, and they do different jobs. The window cap bounds the bill,
    because bytes scanned follow the window. Requiring a narrowing command does
    not reduce bytes scanned at all; it stops a bare ``fields @message`` from
    being run by accident and returning a wall of events that a cached
    ``FilterLogEvents`` window would have answered for free.

    The narrowing check is per language, because each says the same thing its own way:
    ``filter`` in CWLI, ``where`` in PPL and SQL. Checking for the CWLI words alone
    rejected a ``GROUP BY`` that narrows perfectly well.

    Raises:
        ValueError: The window is wider than :data:`MAX_INSIGHTS_WINDOW`, or the
            query has no narrowing command in ``language``.
    """
    window = end_time - start_time
    if window > MAX_INSIGHTS_WINDOW:
        msg = (
            f'Insights window is capped at {MAX_INSIGHTS_WINDOW.days} days to bound what it bills, '
            f'and this one covers {window.days} days'
        )
        raise ValueError(msg)
    if not _NARROWING_PATTERNS[language].search(query):
        wanted = ', '.join(_NARROWING_COMMANDS[language])
        msg = f'A {language} query must narrow with {wanted} rather than reading the whole window'
        raise ValueError(msg)


@dataclass(frozen=True)
class ScanEstimate:
    """What a query is likely to scan, before it runs.

    Attributes:
        bytes_scanned: Estimated bytes read, summed across the groups.
        group_count: Groups the estimate covers.
        unknown_groups: Groups reporting no stored bytes, whose share is missing.
        measured_groups: Groups whose share came from a sample of recent traffic
            rather than from stored bytes over retention.
    """

    bytes_scanned: int
    group_count: int
    unknown_groups: int
    measured_groups: int = 0

    @property
    def gigabytes(self) -> float:
        """The estimate in gigabytes, which is the unit Insights bills in."""
        return self.bytes_scanned / _BYTES_PER_GB

    @property
    def dollars(self) -> float:
        """What the estimate costs at :data:`DOLLARS_PER_GB`."""
        return self.gigabytes * DOLLARS_PER_GB

    def label(self) -> str:
        """One line naming the estimate, its cost, and how it was arrived at."""
        missing = f', {self.unknown_groups} of them reporting no size' if self.unknown_groups else ''
        return (
            f'Estimate ~{self.gigabytes:.3f} GB scanned across {self.group_count} '
            f'group{"s" if self.group_count != 1 else ""}{missing}, roughly ${self.dollars:.3f}. '
            f'{self._basis()}'
        )

    def _basis(self) -> str:
        averaged = self.group_count - self.measured_groups
        if not averaged:
            return f'Measured from {SAMPLE_SLICES} samples spread across the window, so a burst between them reads low.'
        if not self.measured_groups:
            return (
                "Order of magnitude only: it spreads each group's stored bytes evenly over its "
                'retention, so a group whose traffic grew reads low and a quiet one reads high.'
            )
        return (
            f'{self.measured_groups} of them measured from recent traffic, the rest spread over '
            'retention and so good to an order of magnitude.'
        )


def estimate_scan(
    groups: Sequence[LogGroupInfo],
    *,
    window: timedelta,
    now: datetime,
    rates: Mapping[str, float] | None = None,
) -> ScanEstimate:
    """Estimate what a query over ``groups`` will scan across ``window``.

    There is no AWS preflight API. A group with a measured rate from
    :func:`measure_group_rates` is estimated from that rate, which is what makes
    the number worth reading. Without one, the fallback divides stored bytes by
    the span they accumulated over: that was measured against three production
    groups on 2026-08-22 and came out wrong by up to 8x in both directions,
    because compression pushes it low and a lifetime average pushes it high for a
    group that has quietened down.

    Args:
        groups: Groups the query will read.
        window: The query's window.
        now: Current time, used to date a group that never expires.
        rates: Bytes per second per group name, from a sample of recent traffic.
    """
    measured_rates = rates or {}
    window_seconds = max(window.total_seconds(), 0.0)
    window_days = window_seconds / 86400.0
    total = 0.0
    unknown = 0
    measured = 0
    for group in groups:
        rate = measured_rates.get(group.name)
        if rate is not None:
            measured += 1
            total += rate * window_seconds
        elif group.stored_bytes:
            total += group.stored_bytes / _span_days(group, now=now) * window_days
        else:
            unknown += 1
    return ScanEstimate(
        bytes_scanned=int(total),
        group_count=len(groups),
        unknown_groups=unknown,
        measured_groups=measured,
    )


def _span_days(group: LogGroupInfo, *, now: datetime) -> float:
    if group.retention_days:
        return float(group.retention_days)
    if group.created is not None:
        return max((now - group.created).total_seconds() / 86400.0, 1.0)
    return float(ASSUMED_RETENTION_DAYS)


async def measure_group_rates(
    client: Any,
    group_names: Sequence[str],
    *,
    start: datetime,
    end: datetime,
    slices: int = SAMPLE_SLICES,
    concurrency: int = SAMPLE_CONCURRENCY,
) -> dict[str, float]:
    """Sample traffic inside the query's own window, in bytes per second per group.

    Slices are spread across the window rather than taken from one end, because a
    group's rate moves by more than a factor of two inside an hour. A group is
    left out when nothing it logged supports a rate, and the caller falls back to
    the stored-bytes average for it.

    Sampling goes through ``FilterLogEvents``, which is not billed per gigabyte
    the way the query it guards is.
    """
    limiter = asyncio.Semaphore(max(1, concurrency))

    async def sample(name: str, window: tuple[datetime, datetime]) -> float | None:
        async with limiter:
            return await _measure_slice(client, name, start=window[0], end=window[1])

    windows = _sample_windows(start, end, slices=slices)
    async with asyncio.TaskGroup() as group:
        tasks = {
            name: [group.create_task(sample(name, window)) for window in windows] for name in dict.fromkeys(group_names)
        }
    rates: dict[str, float] = {}
    for name, slice_tasks in tasks.items():
        measured = [rate for rate in (task.result() for task in slice_tasks) if rate is not None]
        if measured:
            rates[name] = sum(measured) / len(measured)
    return rates


def _sample_windows(start: datetime, end: datetime, *, slices: int) -> list[tuple[datetime, datetime]]:
    """Spread up to ``slices`` sample windows across ``[start, end)``."""
    span = end - start
    if span <= SAMPLE_SLICE or slices <= 1:
        return [(max(start, end - SAMPLE_SLICE), end)]
    step = span / slices
    length = min(step, SAMPLE_SLICE)
    return [(start + step * index, start + step * index + length) for index in range(slices)]


async def _measure_slice(client: Any, group_name: str, *, start: datetime, end: datetime) -> float | None:
    response = await client.filter_log_events(
        logGroupName=group_name,
        startTime=int(start.timestamp() * 1000),
        endTime=int(end.timestamp() * 1000),
        limit=SAMPLE_LIMIT,
        interleaved=True,
    )
    events = response.get('events', [])
    if not events:
        return None
    measured_bytes = sum(len(event.get('message', '').encode()) + EVENT_OVERHEAD_BYTES for event in events)
    if response.get('nextToken') is None:
        # Nothing was left behind, so the events cover the whole slice even if
        # they all arrived in one burst inside it.
        return measured_bytes / max((end - start).total_seconds(), _MIN_MEASURABLE_SPAN_SECONDS)
    stamps = [event['timestamp'] for event in events]
    span_seconds = (max(stamps) - min(stamps)) / 1000.0
    if span_seconds < _MIN_MEASURABLE_SPAN_SECONDS:
        return None
    return measured_bytes / span_seconds


class InsightsQueryError(RuntimeError):
    """Raised when a query reaches a terminal status other than ``Complete``."""


@dataclass(frozen=True)
class InsightsResult:
    """One completed query: its columns, rows, and what it cost to run.

    Attributes:
        columns: Field names in the order the query returned them.
        rows: One dict per result row, keyed by field name.
        records_matched: Events matching the query.
        records_scanned: Events read to answer it.
        bytes_scanned: Bytes read, which is what Insights bills on.
    """

    columns: tuple[str, ...]
    rows: tuple[dict[str, str], ...]
    records_matched: int
    records_scanned: int
    bytes_scanned: int


class QueryLanguage(StrEnum):
    """Which Insights language a query is written in.

    ``CWLI`` is the original pipe-delimited language and the default, so an existing
    query keeps meaning what it meant. ``SQL`` and ``PPL`` are the OpenSearch languages
    CloudWatch made generally available, and they bring JOIN and sub-queries that the
    local engine deliberately does not implement
    ([ADR 0010](https://tail-cw.kyleking.me/docs/adr/0010-keep-tail-cw-with-a-narrower-scope/)).
    """

    CWLI = 'CWLI'
    PPL = 'PPL'
    SQL = 'SQL'


SOURCE_COMMAND = re.compile(r'\b(?:SOURCE|FROM)\b', re.IGNORECASE)
"""Whether a query names its own log groups, which decides how ``StartQuery`` is called.

``StartQuery`` takes the groups either as a parameter or from a ``SOURCE``/``FROM`` clause
in the query, and rejects being given both. SQL always names them itself; PPL may.
"""


def names_its_own_groups(query: str, language: QueryLanguage) -> bool:
    """Whether ``query`` selects its log groups itself rather than taking them as a parameter."""
    if language is QueryLanguage.CWLI:
        return False
    return language is QueryLanguage.SQL or bool(SOURCE_COMMAND.search(query))


async def run_insights_query(
    client: Any,
    *,
    log_groups: list[str],
    query: str,
    start_time: datetime,
    end_time: datetime,
    limit: int = DEFAULT_LIMIT,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    language: QueryLanguage = QueryLanguage.CWLI,
) -> InsightsResult:
    """Start a Logs Insights query, wait for it, and return its rows.

    Cancelling the caller stops the query rather than leaving it running and billing.

    Args:
        client: An open CloudWatch Logs client, from :meth:`ClientPool.client`.
        log_groups: Groups to query. Must be empty when the query names its own, since
            ``StartQuery`` rejects being told twice.
        query: The query text, in ``language``.
        start_time: Window start.
        end_time: Window end.
        limit: Maximum rows returned.
        poll_seconds: How often to ask whether the query finished.
        language: Which Insights language ``query`` is written in.

    Raises:
        InsightsQueryError: The query failed, timed out, or was cancelled by AWS.
        ValueError: More log groups than Insights accepts, or a group list given for a
            query that selects its own.
    """
    if len(log_groups) > MAX_INSIGHTS_LOG_GROUPS:
        msg = f'Insights accepts at most {MAX_INSIGHTS_LOG_GROUPS} log groups, got {len(log_groups)}'
        raise ValueError(msg)
    selects_own = names_its_own_groups(query, language)
    if selects_own and log_groups:
        msg = f'A {language.value} query naming its own sources cannot also be given log groups'
        raise ValueError(msg)
    if not selects_own and not log_groups:
        msg = f'A {language.value} query needs either log groups or a SOURCE clause'
        raise ValueError(msg)

    kwargs: dict[str, Any] = {
        'startTime': int(start_time.timestamp()),
        'endTime': int(end_time.timestamp()),
        'queryString': query,
        'limit': limit,
    }
    if language is not QueryLanguage.CWLI:
        kwargs['queryLanguage'] = language.value
    if not selects_own:
        kwargs['logGroupNames'] = log_groups
    started = await client.start_query(**kwargs)
    query_id = started['queryId']
    try:
        response = await _poll_until_terminal(client, query_id, poll_seconds=poll_seconds)
    except asyncio.CancelledError:
        await client.stop_query(queryId=query_id)
        raise

    status = response.get('status', 'Unknown')
    if status != 'Complete':
        msg = f'Insights query {query_id} ended as {status}'
        raise InsightsQueryError(msg)
    return _to_result(response)


async def _poll_until_terminal(client: Any, query_id: str, *, poll_seconds: float) -> dict[str, Any]:
    while True:
        response = await client.get_query_results(queryId=query_id)
        if response.get('status', 'Unknown') in _TERMINAL_STATUSES:
            return response
        await asyncio.sleep(poll_seconds)


def _to_result(response: dict[str, Any]) -> InsightsResult:
    rows = tuple(
        {field['field']: field.get('value', '') for field in row if not field['field'].startswith('@ptr')}
        for row in response.get('results', [])
    )
    columns: dict[str, None] = {}
    for row in rows:
        for column in row:
            columns.setdefault(column, None)
    statistics = response.get('statistics', {})

    return InsightsResult(
        columns=tuple(columns),
        rows=rows,
        records_matched=int(statistics.get('recordsMatched', 0)),
        records_scanned=int(statistics.get('recordsScanned', 0)),
        bytes_scanned=int(statistics.get('bytesScanned', 0)),
    )
