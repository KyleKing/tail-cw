"""Tests for CloudWatch Logs Insights query execution."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tail_cw.aws.insights import (
    ASSUMED_RETENTION_DAYS,
    EVENT_OVERHEAD_BYTES,
    MAX_INSIGHTS_LOG_GROUPS,
    SAMPLE_SLICE,
    SAMPLE_SLICES,
    InsightsQueryError,
    QueryLanguage,
    estimate_scan,
    measure_group_rates,
    names_its_own_groups,
    run_insights_query,
    validate_insights_request,
)
from tail_cw.aws.log_groups import LogGroupInfo

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


@pytest.mark.parametrize(
    ('query', 'days', 'expected'),
    [
        ('filter @message like /boom/', 1, None),
        ('pattern @message', 7, None),
        ('fields @message', 1, 'narrow with filter'),
        ('filter @message like /boom/', 30, 'capped at 7 days'),
        ('stats count(*) by bin(1h)', 1, 'narrow with filter'),
    ],
)
def test_validate_insights_request(query, days, expected):
    end = datetime(2026, 8, 21, tzinfo=UTC)
    start = end - timedelta(days=days)

    if expected is None:
        validate_insights_request(query, start, end)
        return
    with pytest.raises(ValueError, match=expected):
        validate_insights_request(query, start, end)


def _group(
    name: str = '/g',
    *,
    stored_bytes: int | None = 7 * 10**9,
    retention_days: int | None = 7,
    created: datetime | None = None,
) -> LogGroupInfo:
    return LogGroupInfo(
        name=name,
        arn=f'arn:{name}',
        stored_bytes=stored_bytes,
        retention_days=retention_days,
        created=created,
    )


@pytest.mark.parametrize(
    ('groups', 'window', 'expected_gb', 'expected_unknown'),
    [
        # 7 GB over 7 days retention is 1 GB a day.
        ([_group()], timedelta(days=1), 1.0, 0),
        ([_group(), _group('/h')], timedelta(hours=12), 1.0, 0),
        # No retention: the span is measured from creation instead.
        ([_group(retention_days=None, created=END - timedelta(days=14))], timedelta(days=1), 0.5, 0),
        # Neither retention nor creation: the assumed span stands in.
        ([_group(retention_days=None)], timedelta(days=ASSUMED_RETENTION_DAYS), 7.0, 0),
        ([_group(stored_bytes=None)], timedelta(days=1), 0.0, 1),
        ([], timedelta(days=1), 0.0, 0),
    ],
)
def test_estimate_scan(groups, window, expected_gb, expected_unknown):
    estimate = estimate_scan(groups, window=window, now=END)

    assert estimate.gigabytes == pytest.approx(expected_gb, rel=0.01)
    assert estimate.unknown_groups == expected_unknown
    assert estimate.group_count == len(groups)


def test_scan_estimate_label_prices_the_window_and_owns_its_error():
    label = estimate_scan([_group()], window=timedelta(days=2), now=END).label()

    assert '~2.000 GB' in label
    assert '$0.010' in label
    assert 'Order of magnitude only' in label


class _FakeSampler:
    """Answers one ``filter_log_events`` sample per group."""

    def __init__(self, samples: dict[str, dict[str, object]]) -> None:
        self._samples = samples
        self.calls: list[str] = []
        self.windows: list[dict[str, Any]] = []

    async def filter_log_events(self, **kwargs: Any) -> dict[str, object]:
        name = str(kwargs['logGroupName'])
        self.calls.append(name)
        self.windows.append(kwargs)
        return self._samples.get(name, {'events': []})


def _sample_events(count: int, *, size: int, spread_ms: int) -> list[dict[str, Any]]:
    step = spread_ms // max(count - 1, 1)
    base = int(END.timestamp() * 1000)
    return [{'message': 'x' * size, 'timestamp': base + index * step} for index in range(count)]


async def test_a_quiet_group_is_measured_over_the_whole_slice() -> None:
    """Everything it logged came back, so the slice is the denominator even for one burst."""
    events = _sample_events(10, size=100, spread_ms=2000)
    client = _FakeSampler({'/g': {'events': events}})

    rates = await measure_group_rates(client, ['/g'], start=END - SAMPLE_SLICE, end=END)

    expected = 10 * (100 + EVENT_OVERHEAD_BYTES) / SAMPLE_SLICE.total_seconds()
    assert rates['/g'] == pytest.approx(expected)
    assert client.calls == ['/g'], 'a window no wider than one slice is sampled once'


async def test_a_wide_window_is_sampled_in_slices_spread_across_it() -> None:
    """One group measured 5,773 to 14,900 B/s inside an hour, so one end of it is not the rate."""
    client = _FakeSampler({'/g': {'events': _sample_events(10, size=100, spread_ms=2000)}})

    await measure_group_rates(client, ['/g'], start=END - timedelta(hours=1), end=END)

    assert len(client.calls) == SAMPLE_SLICES
    starts = sorted(window['startTime'] for window in client.windows)
    assert len(set(starts)) == SAMPLE_SLICES, 'the slices must not sit on top of each other'
    assert max(starts) - min(starts) == pytest.approx(2 * 3_600_000 / SAMPLE_SLICES)


async def test_a_truncated_sample_is_measured_over_the_span_it_reached() -> None:
    """A capped response covers a prefix of the window, so the window would read low."""
    events = _sample_events(100, size=1000, spread_ms=10_000)
    client = _FakeSampler({'/g': {'events': events, 'nextToken': 'more'}})

    rates = await measure_group_rates(client, ['/g'], start=END - SAMPLE_SLICE, end=END)

    stamps = [event['timestamp'] for event in events]
    span = (max(stamps) - min(stamps)) / 1000.0
    assert rates['/g'] == pytest.approx(100 * (1000 + EVENT_OVERHEAD_BYTES) / span)
    assert span < SAMPLE_SLICE.total_seconds(), 'the slice would have read an order of magnitude low'


@pytest.mark.parametrize(
    'sample',
    [
        {'events': []},
        {'events': _sample_events(50, size=10, spread_ms=100), 'nextToken': 'more'},
    ],
    ids=['nothing logged', 'a burst inside one second'],
)
async def test_a_group_with_no_measurable_rate_is_left_out(sample) -> None:
    rates = await measure_group_rates(_FakeSampler({'/g': sample}), ['/g'], start=END - SAMPLE_SLICE, end=END)

    assert '/g' not in rates


def test_a_measured_rate_replaces_the_average_for_that_group_alone() -> None:
    groups = [_group('/measured'), _group('/averaged')]

    estimate = estimate_scan(groups, window=timedelta(days=1), now=END, rates={'/measured': 1000.0})

    # 1 GB a day averaged, plus 1000 B/s measured over a day.
    assert estimate.gigabytes == pytest.approx(1.0 + 86_400_000 / 10**9)
    assert estimate.measured_groups == 1
    assert 'measured from recent traffic' in estimate.label()


@pytest.mark.parametrize(
    ('language', 'query', 'expected'),
    [
        (QueryLanguage.CWLI, 'filter @message like /x/', False),
        (QueryLanguage.CWLI, 'SOURCE logGroups() | filter x', False),
        (QueryLanguage.SQL, 'SELECT * FROM `g` WHERE level = "x"', True),
        (QueryLanguage.PPL, 'source=g | where level="x"', True),
        (QueryLanguage.PPL, 'where level="x" | stats count()', False),
    ],
)
def test_only_a_query_naming_its_sources_forgoes_the_group_parameter(language, query, *, expected):
    """StartQuery takes the groups as a parameter or from the query, and rejects both."""
    assert names_its_own_groups(query, language) is expected


async def test_a_cwli_query_still_sends_its_groups_and_no_language():
    """The default has to keep meaning what it meant, so nothing new goes on the wire."""
    client = _FakeInsightsClient(['Complete'])

    await _run(client)

    assert client.started['logGroupNames'] == ['/g']
    assert 'queryLanguage' not in client.started


async def test_a_sql_query_sends_the_language_and_no_groups():
    client = _FakeInsightsClient(['Complete'])

    await run_insights_query(
        client,
        log_groups=[],
        query='SELECT level FROM `g` WHERE level = "error"',
        start_time=START,
        end_time=END,
        poll_seconds=_NO_POLL_DELAY,
        language=QueryLanguage.SQL,
    )

    assert client.started['queryLanguage'] == 'SQL'
    assert 'logGroupNames' not in client.started, 'AWS rejects being told the groups twice'


async def test_naming_the_groups_twice_is_refused_before_it_bills():
    client = _FakeInsightsClient(['Complete'])

    with pytest.raises(ValueError, match='cannot also be given log groups'):
        await run_insights_query(
            client,
            log_groups=['/g'],
            query='SELECT level FROM `g` WHERE level = "error"',
            start_time=START,
            end_time=END,
            poll_seconds=_NO_POLL_DELAY,
            language=QueryLanguage.SQL,
        )
    assert client.started == {}


async def test_naming_them_nowhere_is_refused_too():
    client = _FakeInsightsClient(['Complete'])

    with pytest.raises(ValueError, match='needs either log groups or a SOURCE clause'):
        await run_insights_query(
            client,
            log_groups=[],
            query='where level="error"',
            start_time=START,
            end_time=END,
            poll_seconds=_NO_POLL_DELAY,
            language=QueryLanguage.PPL,
        )


@pytest.mark.parametrize(
    ('language', 'query', 'accepted'),
    [
        ('CWLI', 'filter @message like /x/', True),
        ('CWLI', 'fields @message', False),
        ('SQL', 'SELECT level, count(*) FROM `g` GROUP BY level', True),
        ('SQL', 'SELECT level, count(*) FROM `g` group  by level', True),
        ('SQL', 'SELECT * FROM `g`', False),
        ('PPL', 'source=g | where level="x"', True),
        ('PPL', 'source=g | fields level', False),
    ],
)
def test_narrowing_is_checked_in_the_language_the_query_is_written_in(language, query, *, accepted):
    """The CWLI words alone rejected a GROUP BY that narrows perfectly well."""
    window = (START, START + timedelta(minutes=5))
    if accepted:
        validate_insights_request(query, *window, language)
        return
    with pytest.raises(ValueError, match='must narrow with'):
        validate_insights_request(query, *window, language)
