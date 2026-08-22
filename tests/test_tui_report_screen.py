"""Interaction tests for the rollup, alarms, Insights, and history views."""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from textual.widgets import Label, Markdown

from tail_cw.aws.alarms import AlarmSummary
from tail_cw.aws.insights import InsightsResult
from tail_cw.aws.log_groups import LogGroupInfo
from tail_cw.cli import Session
from tail_cw.config import TailCWConfig
from tail_cw.history import HistoryKind, load_history, make_entry, save_history
from tail_cw.query.rollup import Granularity, RollupReport, roll_up
from tail_cw.tui.navigation import NavTarget, ViewKind
from tail_cw.tui.report_screen import ALARM_LOOKBACK, ReportKind, ReportScreen
from tail_cw.tui.shell import ShellServices, TailCWApp
from tail_cw.tui.views import build_screen
from tests.asyncsupport import calls, returns
from tests.factories import BASE_TIME, make_events
from tests.tui_support import running

GROUP = '/aws/test/group'


def _session() -> Session:
    return Session(start=BASE_TIME, end=BASE_TIME + timedelta(hours=1), selected_groups=[GROUP], profile='read-prod')


def _alarm(name: str, state: str = 'ALARM') -> AlarmSummary:
    return AlarmSummary(
        name=name,
        state=state,
        state_reason='Threshold Crossed',
        state_updated=datetime(2026, 8, 21, tzinfo=UTC),
        description='',
        namespace='AWS/ECS',
        metric_name='MemoryUtilization',
        dimensions=(),
        statistic='Average',
        comparison='GreaterThanThreshold',
        threshold=80.0,
        period_seconds=60,
        datapoints_to_alarm=1,
        evaluation_periods=1,
        actions_enabled=True,
    )


_RESULT = InsightsResult(
    columns=('day', 'events'),
    rows=({'day': '2026-08-21', 'events': '7'},),
    records_matched=7,
    records_scanned=100,
    bytes_scanned=1_500_000_000,
)


def _rollup(_groups: Sequence[str], start: datetime, end: datetime) -> RollupReport:
    events = make_events(['{"level":"error","logger":"a","event":"disk full"}'] * 3)
    return roll_up(events, window=(start, end), granularity=Granularity.HOUR)


def _services(**overrides) -> ShellServices:
    defaults = {
        'roll_up_logs': calls(_rollup),
        'list_alarms': returns(
            ([_alarm('quiet-alarm'), _alarm('flapping-alarm')], {'flapping-alarm': 52, 'quiet-alarm': 1})
        ),
        'run_insights': returns(_RESULT),
    }
    return ShellServices(**{**defaults, **overrides})


def _app(kind: ReportKind, *payload: str, services: ShellServices | None = None) -> TailCWApp:
    return TailCWApp(
        TailCWConfig(),
        _session(),
        build_screen=build_screen,
        services=services if services is not None else _services(),
        target=NavTarget(kind=ViewKind.REPORT, label=kind.value, payload=(kind.value, *payload)),
    )


def _body(app: TailCWApp) -> str:
    screen = app.screen
    assert isinstance(screen, ReportScreen)
    return screen.query_one('#report', Markdown).source


def _status(app: TailCWApp) -> str:
    return str(app.screen.query_one('#report_status', Label).render())


@pytest.mark.asyncio
async def test_the_rollup_ranks_patterns_from_the_selected_groups():
    app = _app(ReportKind.SUMMARY)

    async with running(app, settled=True):
        assert 'disk full' in _body(app)
        assert 'summary' in _status(app)


@pytest.mark.asyncio
async def test_the_rollup_says_what_to_do_when_nothing_is_selected():
    app = _app(ReportKind.SUMMARY)
    app.session.selected_groups = []

    async with running(app, settled=True):
        assert 'Select log groups first' in _body(app)


@pytest.mark.asyncio
async def test_alarm_transitions_are_read_over_a_useful_lookback():
    """An hour of history reports zero transitions for nearly every alarm."""
    windows: list[tuple[datetime, datetime]] = []

    def list_alarms(start: datetime, end: datetime) -> tuple[list[AlarmSummary], dict[str, int]]:
        windows.append((start, end))
        return [_alarm('flapping-alarm')], {'flapping-alarm': 52}

    app = _app(ReportKind.ALARMS, services=_services(list_alarms=calls(list_alarms)))

    async with running(app, settled=True):
        start, end = windows[-1]
        assert end - start >= ALARM_LOOKBACK
        assert 'changed state' in _body(app)


@pytest.mark.asyncio
async def test_alarms_rank_by_transition_count():
    """The flapping alarm is the finding, so it cannot be sorted below a quiet one."""
    app = _app(ReportKind.ALARMS)

    async with running(app, settled=True):
        body = _body(app)
        assert body.index('flapping-alarm') < body.index('quiet-alarm')
        assert '52' in body


@pytest.mark.asyncio
async def test_insights_reports_what_it_scanned_and_what_it_cost():
    app = _app(ReportKind.INSIGHTS, 'filter @message like /boom/')

    async with running(app, settled=True):
        body = _body(app)
        assert '1.500 GB scanned' in body
        assert '$0.007' in body
        assert '| 2026-08-21 | 7 |' in body


def _big_group() -> LogGroupInfo:
    """A group holding 100 GB a day, so the session's hour clears the 1 GB ceiling."""
    return LogGroupInfo(name=GROUP, arn=f'arn:{GROUP}', stored_bytes=700 * 10**9, retention_days=7, created=None)


@pytest.mark.asyncio
async def test_insights_estimates_the_bill_and_waits_before_running_an_expensive_query():
    ran: list[str] = []

    def run_insights(_groups: Sequence[str], query: str, _start: datetime, _end: datetime) -> InsightsResult:
        ran.append(query)
        return _RESULT

    services = _services(list_groups=returns([_big_group()]), run_insights=calls(run_insights))
    app = _app(ReportKind.INSIGHTS, 'filter @message like /boom/', services=services)

    async with running(app, settled=True) as pilot:
        assert 'Above the 1 GB ceiling' in _body(app)
        assert ran == [], 'the query must not bill before the estimate is answered'
        assert not load_history(), 'an unrun query is not history'

        await pilot.press('y')
        await pilot.pause()

        assert ran == ['filter @message like /boom/']
        assert '1.500 GB scanned' in _body(app)


@pytest.mark.asyncio
async def test_insights_runs_under_the_ceiling_without_asking():
    app = _app(
        ReportKind.INSIGHTS,
        'filter @message like /boom/',
        services=_services(list_groups=returns([_big_group()])),
    )
    app.session.start = app.session.end - timedelta(minutes=5)

    async with running(app, settled=True):
        body = _body(app)
        assert 'Above the' not in body
        assert 'Estimate ~' in body, 'the estimate is worth showing even when it does not stop the query'


@pytest.mark.asyncio
async def test_insights_refuses_a_query_that_reads_the_whole_window():
    app = _app(ReportKind.INSIGHTS, 'fields @message')

    async with running(app, settled=True):
        assert 'narrow with filter' in _body(app)
        assert 'Failed' in _status(app)


@pytest.mark.asyncio
async def test_a_run_lands_in_the_history_the_next_session_reads():
    app = _app(ReportKind.ALARMS)

    async with running(app, settled=True):
        recorded = load_history()

    assert [entry.kind for entry in recorded] == [HistoryKind.ALARMS]
    assert 'flapping-alarm' in recorded[0].detail
    assert recorded[0].profile == 'read-prod'
    assert GROUP in recorded[0].title, 'an entry has to say what it ran against'


@pytest.mark.asyncio
async def test_the_history_view_shows_what_the_cli_recorded(tmp_path: Path):
    save_history(
        [
            make_entry(
                HistoryKind.INSIGHTS,
                recorded=datetime(2026, 8, 21, tzinfo=UTC),
                title='filter @message like /timeout/',
                window='7d',
                detail='| count |\n',
            ),
        ],
    )
    app = _app(ReportKind.HISTORY)

    async with running(app, settled=True):
        assert 'filter @message like /timeout/' in _body(app)


@pytest.mark.asyncio
async def test_the_history_view_says_so_when_nothing_is_recorded():
    app = _app(ReportKind.HISTORY)

    async with running(app, settled=True):
        assert 'Nothing recorded yet' in _body(app)


@pytest.mark.asyncio
async def test_a_missing_service_is_reported_rather_than_raised():
    app = _app(ReportKind.SUMMARY, services=ShellServices())

    async with running(app, settled=True):
        assert 'No rollup service available' in _body(app)
