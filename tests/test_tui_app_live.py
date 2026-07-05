"""Tests for the live streaming mode of LogTailApp."""

from datetime import UTC, datetime, timedelta

import pytest
from textual.widgets import DataTable, Label

from tail_cw.aws.client import LogEvent
from tail_cw.config import TailCWConfig, TUIConfig
from tail_cw.tui.app import LogTailApp

BASE_TIME = datetime(2026, 7, 5, 12, 0, 0, tzinfo=UTC)


def _make_live_events(count: int, *, offset: int = 0) -> list[LogEvent]:
    return [
        LogEvent(
            log_group='/aws/test/group',
            log_stream='stream-live',
            timestamp=BASE_TIME + timedelta(seconds=offset + index),
            message=f'live message {offset + index}',
            event_id=f'live-{offset + index:04d}',
            ingestion_time=None,
        )
        for index in range(count)
    ]


def _make_live_app(*, live_buffer_limit: int = 100) -> LogTailApp:
    config = TailCWConfig(tui=TUIConfig(live_buffer_limit=live_buffer_limit))
    return LogTailApp(config=config, title='CloudWatch Live Tail')


@pytest.mark.asyncio
async def test_live_worker_streams_events_into_table():
    app = _make_live_app()
    events = _make_live_events(5)
    app.start_live_tail(lambda: iter(events))

    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()

        table = app.query_one('#log_table', DataTable)
        assert table.row_count == 5
        assert app._live_event_count == 5
        assert list(app._live_buffer) == events
        assert app._all_events == events
        assert app._live_active is False
        status = app.query_one('#status', Label)
        assert 'Stopped' in str(status.render())
        assert '5 events' in str(status.render())


@pytest.mark.asyncio
async def test_live_batched_flush_and_pause_resume():
    app = _make_live_app(live_buffer_limit=100)

    async with app.run_test() as pilot:
        app._live_stream_factory = lambda: iter([])
        app._live_active = True

        first_batch = _make_live_events(3)
        app._pending_live_events.extend(first_batch)
        app._flush_live_events()

        table = app.query_one('#log_table', DataTable)
        assert table.row_count == 3
        status = app.query_one('#status', Label)
        assert 'Live' in str(status.render())

        await pilot.press('space')
        assert app._live_paused
        assert 'Paused' in str(status.render())

        second_batch = _make_live_events(2, offset=3)
        app._pending_live_events.extend(second_batch)
        app._flush_live_events()

        assert table.row_count == 3
        assert len(app._live_buffer) == 5
        assert app._live_event_count == 5

        await pilot.press('space')
        assert 'Live' in str(status.render())
        assert table.row_count == 5
        assert app._log_events == first_batch + second_batch


@pytest.mark.asyncio
async def test_live_buffer_evicts_oldest_and_rebuilds_table():
    app = _make_live_app(live_buffer_limit=10)

    async with app.run_test() as pilot:
        app._live_stream_factory = lambda: iter([])
        app._live_active = True

        app._pending_live_events.extend(_make_live_events(120))
        app._flush_live_events()
        await pilot.pause()

        table = app.query_one('#log_table', DataTable)
        assert len(app._live_buffer) == 10
        assert table.row_count == 10
        assert app._log_events == _make_live_events(10, offset=110)
        assert app._all_events[-1].event_id == 'live-0119'


@pytest.mark.asyncio
async def test_live_search_filters_buffered_events():
    app = _make_live_app()

    async with app.run_test() as pilot:
        app._live_stream_factory = lambda: iter([])
        app._live_active = True

        app._pending_live_events.extend(_make_live_events(4))
        app._flush_live_events()

        results = app._filter_events_in_memory('live message 2')
        assert [event.event_id for event in results] == ['live-0002']

        assert app._search_input is not None
        app._search_input.value = 'live message'
        app._pending_live_events.extend(_make_live_events(2, offset=4))
        app._flush_live_events()

        table = app.query_one('#log_table', DataTable)
        assert table.row_count == 4
        assert len(app._live_buffer) == 6
        await pilot.pause()


@pytest.mark.asyncio
async def test_live_worker_error_keeps_buffer_browsable():
    app = _make_live_app()

    def failing_stream():
        yield from _make_live_events(2)
        msg = 'session exhausted'
        raise RuntimeError(msg)

    app.start_live_tail(failing_stream)

    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()

        table = app.query_one('#log_table', DataTable)
        assert table.row_count == 2
        assert app._live_active is False
        assert len(app._live_buffer) == 2
        status = app.query_one('#status', Label)
        assert 'Stopped' in str(status.render())


def test_note_live_sampled_sets_flag():
    app = _make_live_app()

    app.note_live_sampled(sampled=True)

    assert app._live_sampled is True
