# ruff: file-ignore[unused-async] - the fakes conform to awaitable service signatures
"""Unit tests for the entry point and the live service wiring."""

from __future__ import annotations

import subprocess  # noqa: S404 - a fresh interpreter is the only honest way to see what an import loads
import sys
from collections.abc import AsyncIterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest

from tail_cw.__main__ import main
from tail_cw.aws.dashboards import Dashboard
from tail_cw.aws.events import LogEvent
from tail_cw.cli import FetchRequest, Session, ShellSeed
from tail_cw.config import TailCWConfig
from tail_cw.demo import DEMO_LOG_GROUP
from tail_cw.services import (
    _build_app,
    _demo_resolve_logs,
    _demo_services,
    _live_services,
    _run_shell,
    _seed_to_target,
    _target_label,
)
from tail_cw.tui.navigation import ViewKind
from tail_cw.tui.shell import ShellServices, TailCWApp

NOW = datetime(2026, 7, 24, 12, 0, 0, tzinfo=UTC)


def _make_session() -> Session:
    return Session(start=NOW - timedelta(hours=1), end=NOW, profile='dev', region='us-west-2')


class _FakePool:
    """Hands out one sentinel per service so wiring can be asserted without AWS."""

    def __init__(self) -> None:
        self.requested: list[str] = []

    async def client(self, service_name: str) -> str:
        self.requested.append(service_name)
        return f'client:{service_name}'


def _live(config: TailCWConfig, session: Session) -> tuple[ShellServices, _FakePool]:
    pool = _FakePool()
    with ThreadPoolExecutor(max_workers=1) as executor, ThreadPoolExecutor(max_workers=1) as fetch_executor:
        return _live_services(config, session, pool, executor, fetch_executor), pool


def test_main_function_exists():
    assert callable(main)


def test_main_without_subcommand_prints_help(capsys):
    result = main([])

    assert result == 2
    assert 'usage: tail-cw' in capsys.readouterr().err


def test_main_returns_run_cli_exit_code():
    with patch('tail_cw.services.run', return_value=0) as mock_run:
        result = main(['logs', '/g'])

    assert result == 0
    mock_run.assert_called_once()


HEAVY_MODULES = ('aiobotocore', 'duckdb', 'polars', 'textual')


def test_the_entry_point_does_not_load_the_heavy_stack():
    """--help must not pay for aiobotocore, Polars, DuckDB, or Textual."""
    probe = f'import sys, tail_cw.__main__;print([name for name in {HEAVY_MODULES!r} if name in sys.modules])'
    loaded = subprocess.run([sys.executable, '-c', probe], capture_output=True, text=True, check=True)  # noqa: S603

    assert loaded.stdout.strip() == '[]'


def test_main_handles_keyboard_interrupt():
    with patch('tail_cw.services.run', side_effect=KeyboardInterrupt()):
        result = main(['logs', '/g'])

    assert result == 0


def test_main_handles_generic_exception(capsys):
    with patch('tail_cw.services.run', side_effect=RuntimeError('test error')):
        result = main(['logs', '/g'])

    assert result == 1
    assert 'test error' in capsys.readouterr().err


def test_main_names_the_failure_inside_a_task_group(capsys):
    """Concurrent fetches wrap the real error, and "1 sub-exception" names nothing."""
    carried = ExceptionGroup('unhandled errors in a TaskGroup', [RuntimeError('token has expired')])
    with patch('tail_cw.services.run', side_effect=carried):
        result = main(['logs', '/g'])

    err = capsys.readouterr().err
    assert result == 1
    assert 'token has expired' in err
    assert 'sub-exception' not in err


def test_main_handles_config_error(tmp_path, capsys):
    config_path = tmp_path / 'config.toml'
    config_path.write_text('not valid toml [', encoding='utf-8')

    result = main(['logs', '/g', '--config', str(config_path)])

    assert result == 1
    assert 'Configuration error' in capsys.readouterr().err


@pytest.mark.parametrize(
    ('seed', 'expected_kind'),
    [
        (ShellSeed(view='groups'), ViewKind.GROUPS),
        (ShellSeed(view='logs', targets=('/group/one',)), ViewKind.LOGS),
        (ShellSeed(view='tail', targets=('/group/one',)), ViewKind.LOGS),
        (ShellSeed(view='dashboards'), ViewKind.DASHBOARDS),
        (ShellSeed(view='dashboard', targets=('prod-overview',)), ViewKind.DASHBOARD),
    ],
)
def test_seed_to_target_kind(seed, expected_kind):
    assert _seed_to_target(seed).kind == expected_kind


def test_seed_to_target_tail_label_marks_live_mode():
    target = _seed_to_target(ShellSeed(view='tail', targets=('/group/one',)))

    assert target.label.startswith('tail')
    assert target.payload == ('/group/one',)


def test_seed_to_target_logs_label_is_not_live():
    target = _seed_to_target(ShellSeed(view='logs', targets=('/group/one',)))

    assert not target.label.startswith('tail')
    assert target.label.startswith('logs')


def test_seed_to_target_dashboard_without_targets_falls_back():
    target = _seed_to_target(ShellSeed(view='dashboard'))

    assert target.payload == ('dashboard',)
    assert target.label == 'dashboard'


def test_target_label_single_group():
    assert _target_label(ShellSeed(view='logs', targets=('/aws/lambda/api',))) == '/aws/lambda/api'


def test_target_label_several_groups():
    assert _target_label(ShellSeed(view='logs', targets=('/one', '/two', '/three'))) == '3 groups'


async def test_demo_services_load_dashboard_needs_no_aws():
    services = _demo_services()

    assert services.load_dashboard is not None
    assert isinstance(await services.load_dashboard('anything'), Dashboard)


async def test_demo_services_lists_the_demo_dashboard():
    services = _demo_services()

    assert services.list_dashboards is not None
    summaries = await services.list_dashboards()
    assert [summary.name for summary in summaries] == ['demo']


def test_demo_services_are_callable():
    services = _demo_services()
    populated = {field.name for field in fields(ShellServices) if getattr(services, field.name) is not None}

    assert populated == {
        'count_events',
        'fetch_metrics',
        'list_dashboards',
        'list_groups',
        'load_dashboard',
        'log_volume',
        'resolve_logs',
        'roll_up_logs',
    }
    assert all(callable(getattr(services, name)) for name in populated)


async def test_demo_services_list_groups_resolves_the_demo_group():
    services = _demo_services()

    assert services.list_groups is not None
    groups = await services.list_groups()
    assert [info.name for info in groups] == [DEMO_LOG_GROUP]


def test_demo_resolve_logs_writes_parquet_files():
    paths = _demo_resolve_logs(['/aws/lambda/api'], NOW - timedelta(hours=1), NOW)

    assert paths
    assert all(path.exists() for path in paths)
    assert all(path.suffix == '.parquet' for path in paths)


def test_demo_resolve_logs_defaults_to_the_demo_group():
    paths = _demo_resolve_logs([], NOW - timedelta(hours=1), NOW)

    assert len(paths) == 1


def test_live_services_populates_every_service():
    """A live dashboard gets the same service set the demo does, log volume included."""
    services, _ = _live(TailCWConfig(), _make_session())
    unset = {field.name for field in fields(ShellServices) if getattr(services, field.name) is None}

    assert unset == set()


def test_live_services_are_callable_without_touching_aws():
    services, _ = _live(TailCWConfig(), _make_session())
    populated = [field.name for field in fields(ShellServices) if getattr(services, field.name) is not None]

    assert all(callable(getattr(services, name)) for name in populated)


async def test_live_services_fetch_metrics_uses_the_pooled_cloudwatch_client():
    session = _make_session()
    services, pool = _live(TailCWConfig(), session)
    recorded: list[dict[str, object]] = []

    async def fake_fetch(client, queries, start_time, end_time):
        recorded.append({'client': client, 'queries': queries, 'start': start_time, 'end': end_time})
        return []

    assert services.fetch_metrics is not None
    with patch('tail_cw.services.fetch_metric_data', fake_fetch):
        assert await services.fetch_metrics([{'Id': 'm1'}], session.start, session.end) == []

    assert recorded[0]['client'] == 'client:cloudwatch'
    assert pool.requested == ['cloudwatch']


async def test_live_services_count_events_caps_the_scan():
    session = _make_session()
    services, _ = _live(TailCWConfig(), session)
    events = [
        LogEvent(
            log_group='/g',
            log_stream='s',
            timestamp=NOW,
            message='m',
            ingestion_time=None,
        )
        for index in range(3)
    ]

    async def fake_fetch(*_args: Any, **_kwargs: Any) -> AsyncIterator[LogEvent]:
        for event in events:
            yield event

    assert services.count_events is not None
    with patch('tail_cw.services.fetch_log_events', fake_fetch):
        assert await services.count_events('/g', session.start, session.end) == 3


async def test_a_fetch_and_a_query_do_not_share_a_thread_pool(tmp_path):
    """Four groups fetching filled the shared pool, and a search then waited for the fetch."""
    session = _make_session()
    config = TailCWConfig()
    config.cache.cache_dir = tmp_path / 'cache'
    pool = _FakePool()
    seen: dict[str, object] = {}

    async def fake_resolve(_client, _requests, _config, **kwargs):
        seen['fetch'] = kwargs['executor']
        return []

    with ThreadPoolExecutor(max_workers=1) as queries, ThreadPoolExecutor(max_workers=1) as fetches:
        services = _live_services(config, session, pool, queries, fetches)
        assert services.resolve_logs is not None
        assert services.load_traces is not None
        with patch('tail_cw.services.resolve_parquet_paths', fake_resolve):
            await services.resolve_logs(['/one'], session.start, session.end)
        await services.load_traces([], None, [], None)

        assert seen['fetch'] is fetches
        assert queries is not fetches


async def test_live_services_resolve_logs_builds_one_request_per_group(tmp_path):
    session = _make_session()
    config = TailCWConfig()
    config.cache.cache_dir = tmp_path / 'cache'
    services, _ = _live(config, session)
    recorded: list[Sequence[FetchRequest]] = []

    async def fake_resolve(_client, requests, _config, **_kwargs):
        recorded.append(requests)
        return []

    assert services.resolve_logs is not None
    with patch('tail_cw.services.resolve_parquet_paths', fake_resolve):
        await services.resolve_logs(['/one', '/two'], session.start, session.end)

    requests = recorded[0]
    assert [request.log_group for request in requests] == ['/one', '/two']
    assert {request.profile for request in requests} == {'dev'}


def _run_shell_without_a_terminal(
    monkeypatch,
    config: TailCWConfig,
    session: Session,
    seed: ShellSeed,
) -> TailCWApp:
    started: list[TailCWApp] = []

    async def fake_run_async(app: TailCWApp) -> None:
        started.append(app)

    monkeypatch.setattr(TailCWApp, 'run_async', fake_run_async)
    _run_shell(config, session, seed)
    assert len(started) == 1
    return started[0]


def test_run_shell_builds_the_app_with_the_seeded_target(monkeypatch):
    config = TailCWConfig()
    session = _make_session()

    app = _run_shell_without_a_terminal(
        monkeypatch,
        config,
        session,
        ShellSeed(view='tail', targets=('/group/one',)),
    )

    assert app.config_data is config
    assert app.session is session
    assert app.nav.stack[-1].kind == ViewKind.LOGS
    assert app.nav.stack[-1].label.startswith('tail')
    assert app.services.live_stream is not None


def test_run_shell_uses_demo_services_for_a_demo_seed(monkeypatch):
    app = _run_shell_without_a_terminal(
        monkeypatch,
        TailCWConfig(),
        _make_session(),
        ShellSeed(view='dashboard', targets=('demo',), demo=True),
    )

    assert app.services.live_stream is None
    assert app.services.log_volume is not None
    assert app.nav.stack[-1].kind == ViewKind.DASHBOARD


def test_opening_a_log_view_counts_as_selecting_its_groups():
    """The rollup and Insights views act on the selection, so a CLI seed must fill it."""
    session = _make_session()
    seed = ShellSeed(view='logs', targets=('/aws/lambda/api', '/aws/lambda/worker'))

    app = _build_app(TailCWConfig(), session, seed, ShellServices())

    assert app.session.selected_groups == ['/aws/lambda/api', '/aws/lambda/worker']
