"""Tests for the dive confirmation screen and its pure helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from textual.app import App
from textual.widgets import OptionList

from tail_cw.aws.dashboards import DiveCandidate, LogWidget, MetricWidget, TextWidget, WidgetLayout
from tail_cw.cli import Session
from tail_cw.config import load_config
from tail_cw.tui.dive_screen import DiveConfirmScreen, candidate_label, widget_filter_text
from tail_cw.tui.navigation import NavTarget, ViewKind
from tail_cw.tui.shell import ShellServices, TailCWApp
from tail_cw.tui.views import build_screen

_NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)


def _log_widget(query: str) -> LogWidget:
    return LogWidget(layout=WidgetLayout(), title='api logs', query=query)


def _candidates() -> list[DiveCandidate]:
    return [
        DiveCandidate(log_group='/aws/lambda/api', reason='SOURCE clause', exists=True, event_count=12),
        DiveCandidate(log_group='/ecs/web/api', reason='ClusterName dimension', exists=False, event_count=None),
    ]


def _host() -> TailCWApp:
    return TailCWApp(
        load_config(),
        Session(start=_NOW - timedelta(hours=1), end=_NOW),
        build_screen=build_screen,
        services=ShellServices(),
        target=NavTarget(kind=ViewKind.DASHBOARD, label='demo', payload=('demo',)),
    )


def test_widget_filter_text_reads_a_filter_clause() -> None:
    widget = _log_widget("SOURCE '/aws/lambda/api' | filter level = 'ERROR' | fields @message")
    assert widget_filter_text(widget) == "level = 'ERROR'"


def test_widget_filter_text_is_none_without_a_filter_clause() -> None:
    assert widget_filter_text(_log_widget("SOURCE '/aws/lambda/api' | fields @message")) is None


def test_widget_filter_text_is_none_for_other_widget_kinds() -> None:
    metric = MetricWidget(layout=WidgetLayout(), title='m', view='timeSeries', metrics=[])
    assert widget_filter_text(metric) is None
    assert widget_filter_text(TextWidget(layout=WidgetLayout(), markdown='# hi')) is None


def test_candidate_label_names_the_evidence() -> None:
    exists, missing = _candidates()
    assert '12 events in window' in candidate_label(exists)
    assert 'SOURCE clause' in candidate_label(exists)
    assert 'not found in this account' in candidate_label(missing)


def test_candidate_label_for_an_uncounted_existing_group() -> None:
    candidate = DiveCandidate(log_group='g', reason='ApiId dimension', exists=True, event_count=None)
    assert candidate_label(candidate).endswith('exists')


@pytest.mark.asyncio
async def test_confirmation_preselects_the_top_candidate() -> None:
    widget = _log_widget("SOURCE '/aws/lambda/api' | fields @message")
    app = _host()
    async with app.run_test(size=(120, 32)) as pilot:
        await pilot.pause()
        screen = DiveConfirmScreen(widget, _candidates())
        app.push_screen(screen)
        await pilot.pause()
        options = screen.query_one('#dive_candidates', OptionList)
        assert options.highlighted == 0
        assert options.option_count == 2


@pytest.mark.asyncio
async def test_enter_opens_the_highlighted_group_and_carries_the_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[list[str]] = []

    def fake_open_logs(self: TailCWApp, groups: list[str], *, live: bool = False) -> None:
        del self, live
        opened.append(list(groups))

    monkeypatch.setattr(TailCWApp, 'open_logs', fake_open_logs)
    widget = _log_widget("SOURCE '/aws/lambda/api' | filter status >= 500")
    app = _host()
    async with app.run_test(size=(120, 32)) as pilot:
        await pilot.pause()
        app.push_screen(DiveConfirmScreen(widget, _candidates()))
        await pilot.pause()
        await pilot.press('j')
        await pilot.press('enter')
        await pilot.pause()
        assert opened == [['/ecs/web/api']]
        assert app.session.filter_pattern == 'status >= 500'


@pytest.mark.asyncio
async def test_escape_cancels_the_confirmation() -> None:
    widget = _log_widget("SOURCE '/aws/lambda/api' | fields @message")
    app = _host()
    async with app.run_test(size=(120, 32)) as pilot:
        await pilot.pause()
        app.push_screen(DiveConfirmScreen(widget, _candidates()))
        await pilot.pause()
        await pilot.press('escape')
        await pilot.pause()
        assert not isinstance(app.screen, DiveConfirmScreen)


@pytest.mark.asyncio
async def test_open_candidate_ignores_an_out_of_range_index() -> None:
    widget = _log_widget("SOURCE '/aws/lambda/api' | fields @message")
    app = _host()
    async with app.run_test(size=(120, 32)) as pilot:
        await pilot.pause()
        screen = DiveConfirmScreen(widget, _candidates())
        app.push_screen(screen)
        await pilot.pause()
        screen.open_candidate(9)
        await pilot.pause()
        assert isinstance(app.screen, DiveConfirmScreen)


@pytest.mark.asyncio
async def test_open_candidate_requires_a_shell_host() -> None:
    widget = _log_widget("SOURCE '/aws/lambda/api' | fields @message")
    screen = DiveConfirmScreen(widget, _candidates())
    app: App[None] = App()
    async with app.run_test(size=(120, 32)) as pilot:
        app.push_screen(screen)
        await pilot.pause()
        with pytest.raises(TypeError, match='TailCWApp'):
            screen.open_candidate(0)
