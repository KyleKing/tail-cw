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
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
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

_BYTES_PER_GB = 1_000_000_000
_TERMINAL_STATUSES = frozenset({'Complete', 'Failed', 'Cancelled', 'Timeout', 'Unknown'})
_NARROWING_COMMANDS = re.compile(r'(?<![\w@])(filter|pattern|dedup)(?![\w@])', re.IGNORECASE)


def validate_insights_request(query: str, start_time: datetime, end_time: datetime) -> None:
    """Check a query before it is allowed to bill.

    Two guards, and they do different jobs. The window cap bounds the bill,
    because bytes scanned follow the window. Requiring a narrowing command does
    not reduce bytes scanned at all; it stops a bare ``fields @message`` from
    being run by accident and returning a wall of events that a cached
    ``FilterLogEvents`` window would have answered for free.

    Raises:
        ValueError: The window is wider than :data:`MAX_INSIGHTS_WINDOW`, or the
            query has no narrowing command.
    """
    window = end_time - start_time
    if window > MAX_INSIGHTS_WINDOW:
        msg = (
            f'Insights window is capped at {MAX_INSIGHTS_WINDOW.days} days to bound what it bills, '
            f'and this one covers {window.days} days'
        )
        raise ValueError(msg)
    if not _NARROWING_COMMANDS.search(query):
        msg = 'Insights queries must narrow with filter, pattern, or dedup rather than reading the whole window'
        raise ValueError(msg)


@dataclass(frozen=True)
class ScanEstimate:
    """What a query is likely to scan, before it runs.

    Attributes:
        bytes_scanned: Estimated bytes read, summed across the groups.
        group_count: Groups the estimate covers.
        unknown_groups: Groups reporting no stored bytes, whose share is missing.
    """

    bytes_scanned: int
    group_count: int
    unknown_groups: int

    @property
    def gigabytes(self) -> float:
        """The estimate in gigabytes, which is the unit Insights bills in."""
        return self.bytes_scanned / _BYTES_PER_GB

    @property
    def dollars(self) -> float:
        """What the estimate costs at :data:`DOLLARS_PER_GB`."""
        return self.gigabytes * DOLLARS_PER_GB

    def label(self) -> str:
        """One line naming the estimate, its cost, and how far off it can be."""
        missing = f', {self.unknown_groups} of them reporting no size' if self.unknown_groups else ''
        return (
            f'Estimate ~{self.gigabytes:.3f} GB scanned across {self.group_count} '
            f'group{"s" if self.group_count != 1 else ""}{missing}, roughly ${self.dollars:.3f}. '
            "Order of magnitude only: it spreads the group's stored bytes evenly over its "
            'retention, so a group whose traffic grew reads low and a quiet one reads high.'
        )


def estimate_scan(groups: Sequence[LogGroupInfo], *, window: timedelta, now: datetime) -> ScanEstimate:
    """Estimate what a query over ``groups`` will scan across ``window``.

    There is no AWS preflight API, so the estimate divides each group's stored
    bytes by the span they accumulated over and multiplies by the window. A
    group that never expires is measured from its creation time, and one
    reporting neither gets :data:`ASSUMED_RETENTION_DAYS`.

    Measured against three production groups on 2026-08-22, the result was out
    by up to 8x in both directions: compression pushes it low, and averaging
    over a lifetime pushes it high for a group that has quietened down. It is
    worth showing as a scale, and not worth trusting as a number.
    """
    window_days = max(window.total_seconds() / 86400.0, 0.0)
    total = 0.0
    unknown = 0
    for group in groups:
        if not group.stored_bytes:
            unknown += 1
            continue
        total += group.stored_bytes / _span_days(group, now=now) * window_days
    return ScanEstimate(bytes_scanned=int(total), group_count=len(groups), unknown_groups=unknown)


def _span_days(group: LogGroupInfo, *, now: datetime) -> float:
    if group.retention_days:
        return float(group.retention_days)
    if group.created is not None:
        return max((now - group.created).total_seconds() / 86400.0, 1.0)
    return float(ASSUMED_RETENTION_DAYS)


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


async def run_insights_query(
    client: Any,
    *,
    log_groups: list[str],
    query: str,
    start_time: datetime,
    end_time: datetime,
    limit: int = DEFAULT_LIMIT,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
) -> InsightsResult:
    """Start a Logs Insights query, wait for it, and return its rows.

    Cancelling the caller stops the query rather than leaving it running and billing.

    Raises:
        InsightsQueryError: The query failed, timed out, or was cancelled by AWS.
        ValueError: More log groups than Insights accepts in one query.
    """
    if len(log_groups) > MAX_INSIGHTS_LOG_GROUPS:
        msg = f'Insights accepts at most {MAX_INSIGHTS_LOG_GROUPS} log groups, got {len(log_groups)}'
        raise ValueError(msg)

    started = await client.start_query(
        logGroupNames=log_groups,
        startTime=int(start_time.timestamp()),
        endTime=int(end_time.timestamp()),
        queryString=query,
        limit=limit,
    )
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
