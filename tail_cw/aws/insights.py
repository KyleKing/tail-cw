"""Run CloudWatch Logs Insights queries.

Insights answers aggregation questions server-side ("count these by day for a week") that
``FilterLogEvents`` can only answer by downloading every matching event first. It is not the
default path anywhere in tail-cw because it bills per gigabyte scanned while
``FilterLogEvents`` does not, so every result carries the scanned volume for the caller to
report.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Any

DEFAULT_LIMIT = 1000
DEFAULT_POLL_SECONDS = 0.5
MAX_INSIGHTS_LOG_GROUPS = 50
_TERMINAL_STATUSES = frozenset({'Complete', 'Failed', 'Cancelled', 'Timeout', 'Unknown'})


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
