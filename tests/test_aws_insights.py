"""Tests for CloudWatch Logs Insights query execution."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from tail_cw.aws.insights import (
    MAX_INSIGHTS_LOG_GROUPS,
    InsightsQueryError,
    run_insights_query,
)

START = datetime(2026, 8, 14, tzinfo=UTC)
END = START + timedelta(days=7)
_NO_POLL_DELAY = 0.0


class _FakeInsightsClient:
    """Returns one queued status per `get_query_results` call."""

    def __init__(self, statuses: list[str], results: list[list[dict[str, str]]] | None = None) -> None:
        self.statuses = statuses
        self.results = results or []
        self.started: dict[str, object] = {}
        self.stopped: list[str] = []
        self.polls = 0

    async def start_query(self, **kwargs: object) -> dict[str, str]:
        self.started = kwargs
        return {'queryId': 'q-1'}

    async def get_query_results(self, *, queryId: str) -> dict[str, object]:  # noqa: N803
        del queryId
        self.polls += 1
        status = self.statuses[min(self.polls - 1, len(self.statuses) - 1)]
        return {
            'status': status,
            'results': self.results if status == 'Complete' else [],
            'statistics': {'recordsMatched': 2, 'recordsScanned': 100, 'bytesScanned': 2_000_000_000},
        }

    async def stop_query(self, *, queryId: str) -> dict[str, bool]:  # noqa: N803
        self.stopped.append(queryId)
        return {'success': True}


async def _run(client: _FakeInsightsClient, **kwargs: object):
    return await run_insights_query(
        client,
        log_groups=['/g'],
        query='stats count(*) by bin(1d)',
        start_time=START,
        end_time=END,
        poll_seconds=_NO_POLL_DELAY,
        **kwargs,  # type: ignore[arg-type]
    )


async def test_run_insights_query_polls_until_complete_and_reports_cost():
    rows = [
        [{'field': 'day', 'value': '2026-08-14'}, {'field': 'events', 'value': '7'}, {'field': '@ptr', 'value': 'x'}],
        [{'field': 'day', 'value': '2026-08-15'}, {'field': 'events', 'value': '9'}],
    ]
    client = _FakeInsightsClient(['Scheduled', 'Running', 'Complete'], rows)

    result = await _run(client)

    assert client.polls == 3
    # Insights takes epoch seconds, not the milliseconds the log APIs use.
    assert client.started['startTime'] == int(START.timestamp())
    assert client.started['endTime'] == int(END.timestamp())
    # @ptr is an opaque row pointer, so it is not a result column.
    assert result.columns == ('day', 'events')
    assert result.rows == ({'day': '2026-08-14', 'events': '7'}, {'day': '2026-08-15', 'events': '9'})
    assert (result.records_matched, result.records_scanned, result.bytes_scanned) == (2, 100, 2_000_000_000)


@pytest.mark.parametrize('status', ['Failed', 'Cancelled', 'Timeout', 'Unknown'])
async def test_run_insights_query_raises_on_a_terminal_failure(status):
    with pytest.raises(InsightsQueryError, match=status):
        await _run(_FakeInsightsClient([status]))


async def test_run_insights_query_stops_the_query_when_cancelled():
    """A query left running keeps billing, so cancelling the caller must stop it."""
    client = _FakeInsightsClient(['Running'])
    task = asyncio.create_task(_run(client))
    while client.polls == 0:  # noqa: ASYNC110 - waiting on the fake, not on I/O
        await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert client.stopped == ['q-1']


async def test_run_insights_query_rejects_more_groups_than_insights_accepts():
    client = _FakeInsightsClient(['Complete'])
    groups = [f'/g{index}' for index in range(MAX_INSIGHTS_LOG_GROUPS + 1)]

    with pytest.raises(ValueError, match=str(MAX_INSIGHTS_LOG_GROUPS)):
        await run_insights_query(
            client,
            log_groups=groups,
            query='q',
            start_time=START,
            end_time=END,
        )

    assert client.started == {}
