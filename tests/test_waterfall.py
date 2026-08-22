# ruff: file-ignore[unused-async] - the service fakes conform to awaitable signatures
"""Cover the waterfall geometry and the screen that paints it."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from textual.widgets import DataTable, Label

from tail_cw.aws.xray import XRaySpan, XRayTrace
from tail_cw.cli import Session
from tail_cw.config import TailCWConfig
from tail_cw.tui.navigation import NavTarget, ViewKind
from tail_cw.tui.shell import ShellServices, TailCWApp
from tail_cw.tui.views import build_screen
from tail_cw.tui.waterfall_screen import WaterfallScreen, duration_label, row_style, trace_headline
from tail_cw.waterfall import indented_name, render_bar, slowest_chain, waterfall_rows
from tests.tui_support import running

BASE = datetime(2026, 8, 22, 12, 0, 0, tzinfo=UTC)
TRACE_ID = '1-96efc44a-447900ff4e2e2ccec98c47f1'


def _span(
    span_id: str,
    *,
    parent: str | None = None,
    start_ms: int = 0,
    length_ms: int | None = 100,
    service: str = 'irm-api',
    name: str | None = None,
    inferred: bool = False,
    fault: bool = False,
) -> XRaySpan:
    start = BASE + timedelta(milliseconds=start_ms)
    return XRaySpan(
        trace_id=TRACE_ID,
        span_id=span_id,
        parent_span_id=parent,
        name=name if name is not None else span_id,
        start_time=start,
        end_time=None if length_ms is None else start + timedelta(milliseconds=length_ms),
        service_name=service,
        origin=None,
        namespace=None,
        is_fault=fault,
        is_error=False,
        is_throttle=False,
        is_inferred=inferred,
        http_status=None,
        sql_url=None,
        error_message=None,
        annotations=(),
    )


def _trace(*spans: XRaySpan, limit_exceeded: bool = False) -> XRayTrace:
    return XRayTrace(trace_id=TRACE_ID, duration_seconds=None, limit_exceeded=limit_exceeded, spans=spans)


def test_children_nest_under_their_parent_and_sort_by_start() -> None:
    trace = _trace(
        _span('root', length_ms=400),
        _span('late', parent='root', start_ms=200, length_ms=100),
        _span('early', parent='root', start_ms=50, length_ms=100),
        _span('leaf', parent='early', start_ms=60, length_ms=20),
    )

    rows = waterfall_rows(trace)

    assert [(row.span.span_id, row.depth) for row in rows] == [
        ('root', 0),
        ('early', 1),
        ('leaf', 2),
        ('late', 1),
    ]
    early = next(row for row in rows if row.span.span_id == 'early')
    assert early.offset == pytest.approx(0.125), 'a child 50ms into a 400ms trace starts an eighth across'
    assert early.extent == pytest.approx(0.25)


def test_a_span_whose_parent_was_truncated_away_still_appears() -> None:
    """X-Ray drops segments from a large trace, and a lost parent must not lose the child."""
    trace = _trace(_span('orphan', parent='gone-with-the-truncation'))

    rows = waterfall_rows(trace)

    assert [(row.span.span_id, row.depth) for row in rows] == [('orphan', 0)]


def test_the_slowest_chain_follows_the_widest_child_at_every_step() -> None:
    trace = _trace(
        _span('root', length_ms=500),
        _span('fast', parent='root', start_ms=10, length_ms=20),
        _span('slow', parent='root', start_ms=30, length_ms=400),
        _span('slow-child', parent='slow', start_ms=40, length_ms=380),
    )

    assert slowest_chain(trace) == ('root', 'slow', 'slow-child')
    marked = {row.span.span_id for row in waterfall_rows(trace) if row.on_slowest_chain}
    assert marked == {'root', 'slow', 'slow-child'}


def test_a_trace_of_instants_gets_full_width_rows_rather_than_none() -> None:
    trace = _trace(_span('a', length_ms=0), _span('b', length_ms=0))

    rows = waterfall_rows(trace)

    assert [row.extent for row in rows] == pytest.approx([1.0, 1.0])
    assert waterfall_rows(_trace()) == []


def test_a_bar_narrower_than_a_character_still_gets_one() -> None:
    """A span that took no measurable time is not the same as a span that is not there."""
    trace = _trace(_span('root', length_ms=1000), _span('blip', parent='root', start_ms=999, length_ms=1))

    bars = [render_bar(row, width=10) for row in waterfall_rows(trace)]

    assert bars[0] == '██████████'
    assert bars[1] == '         █'
    assert all(len(bar) == 10 for bar in bars), 'every bar pads to the column width'
    assert not render_bar(waterfall_rows(trace)[0], width=0)


def test_a_long_span_name_keeps_its_tail() -> None:
    row = waterfall_rows(_trace(_span('a', name='hatchet.run/PostgresMessageQueue.SendMessage')))[0]

    assert indented_name(row, width=20) == '…geQueue.SendMessage'
    assert len(indented_name(row, width=20)) == 20


def test_depth_indents_the_name() -> None:
    trace = _trace(_span('root'), _span('child', parent='root', name='q'))

    assert [indented_name(row, width=20) for row in waterfall_rows(trace)] == ['root', '  q']


def test_a_row_reads_by_colour_before_it_reads_by_column() -> None:
    trace = _trace(
        _span('root', length_ms=400),
        _span('bad', parent='root', fault=True),
        _span('guessed', parent='root', inferred=True),
    )
    rows = {row.span.span_id: row for row in waterfall_rows(trace)}

    assert row_style(rows['bad']) == 'red'
    assert row_style(rows['guessed']) == 'dim', "an inferred span is the caller's view, not the work's own"
    assert row_style(rows['root']) == 'bold', 'the slowest chain carries the weight'


def test_durations_switch_units_at_a_second() -> None:
    assert duration_label(_span('a', length_ms=999)) == '999ms'
    assert duration_label(_span('a', length_ms=1500)) == '1.50s'
    assert duration_label(_span('a', length_ms=None)) == 'open'


def test_the_headline_names_the_widest_span_and_any_truncation() -> None:
    trace = _trace(_span('root', length_ms=400), _span('q', parent='root', length_ms=20), limit_exceeded=True)

    assert trace_headline(trace) == '2 spans · 1 services · 0 errors · widest root at 400ms · truncated by X-Ray'
    assert 'no segments' in trace_headline(_trace())


def _app(services: ShellServices) -> TailCWApp:
    return TailCWApp(
        TailCWConfig(),
        Session(start=BASE - timedelta(hours=1), end=BASE),
        build_screen=build_screen,
        services=services,
        target=NavTarget(kind=ViewKind.XRAY, label=f'xray {TRACE_ID}', argument=TRACE_ID),
    )


def _status(app: TailCWApp) -> str:
    return str(app.screen.query_one('#waterfall_status', Label).render())


async def test_the_screen_draws_the_fetched_trace_and_s_hides_the_inferred_spans() -> None:
    """A binding is not covered until a test presses the key."""
    trace = _trace(
        _span('root', length_ms=400),
        _span('guessed', parent='root', start_ms=10, length_ms=20, inferred=True),
    )

    async def fetch(trace_id: str) -> XRayTrace:
        assert trace_id == TRACE_ID
        return trace

    app = _app(ShellServices(fetch_xray_trace=fetch))
    async with running(app, settled=True) as pilot:
        table = app.screen.query_one('#waterfall', DataTable)
        assert table.row_count == 2
        assert '2 spans' in _status(app)

        await pilot.press('s')
        await pilot.pause()

        assert table.row_count == 1
        assert '1 inferred hidden' in _status(app)

        await pilot.press('s')
        await pilot.pause()
        assert table.row_count == 2


async def test_a_missing_trace_reports_itself_without_taking_the_app_down() -> None:
    """The id is in the message, and a bare string would render it as Rich markup."""

    async def fetch(trace_id: str) -> XRayTrace:
        msg = f'X-Ray has no segments for [{trace_id}]'
        raise LookupError(msg)

    app = _app(ShellServices(fetch_xray_trace=fetch))
    async with running(app, settled=True) as pilot:
        del pilot
        assert isinstance(app.screen, WaterfallScreen)
        assert 'no segments for [1-96efc44a' in _status(app)


async def test_without_credentials_the_screen_says_so() -> None:
    app = _app(ShellServices())
    async with running(app, settled=True) as pilot:
        del pilot
        assert 'X-Ray is unavailable' in _status(app)
