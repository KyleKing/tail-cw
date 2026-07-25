"""Unit tests for the __main__ entry point (shell wiring and exit codes)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from tail_cw.__main__ import (
    _demo_resolve_logs,
    _demo_services,
    _live_services,
    _run_shell,
    _seed_to_target,
    _target_label,
    main,
)
from tail_cw.aws.client import LogEvent
from tail_cw.aws.dashboards import Dashboard
from tail_cw.cli import FetchRequest, Session, ShellSeed
from tail_cw.config import TailCWConfig
from tail_cw.tui.navigation import ViewKind
from tail_cw.tui.shell import ShellServices, TailCWApp

NOW = datetime(2026, 7, 24, 12, 0, 0, tzinfo=UTC)


def _make_session() -> Session:
    return Session(start=NOW - timedelta(hours=1), end=NOW, profile='dev', region='us-west-2')


def test_main_function_exists():
    assert callable(main)


def test_main_without_subcommand_prints_help(capsys):
    result = main([])

    assert result == 2
    assert 'usage: tail-cw' in capsys.readouterr().err


def test_main_returns_run_cli_exit_code():
    with patch('tail_cw.__main__.run_cli', return_value=0) as mock_run:
        result = main(['logs', '/g'])

    assert result == 0
    mock_run.assert_called_once()


def test_main_handles_keyboard_interrupt():
    with patch('tail_cw.__main__.run_cli', side_effect=KeyboardInterrupt()):
        result = main(['logs', '/g'])

    assert result == 0


def test_main_handles_generic_exception(capsys):
    with patch('tail_cw.__main__.run_cli', side_effect=RuntimeError('test error')):
        result = main(['logs', '/g'])

    assert result == 1
    assert 'test error' in capsys.readouterr().err


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


def test_demo_services_load_dashboard_needs_no_aws():
    services = _demo_services()

    assert services.load_dashboard is not None
    assert isinstance(services.load_dashboard('anything'), Dashboard)


def test_demo_services_lists_the_demo_dashboard():
    services = _demo_services()

    assert services.list_dashboards is not None
    summaries = services.list_dashboards()
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
    }
    assert all(callable(getattr(services, name)) for name in populated)


def test_demo_resolve_logs_writes_parquet_files():
    paths = _demo_resolve_logs(['/aws/lambda/api'], NOW - timedelta(hours=1), NOW, None)

    assert paths
    assert all(path.exists() for path in paths)
    assert all(path.suffix == '.parquet' for path in paths)


def test_demo_resolve_logs_defaults_to_the_demo_group():
    paths = _demo_resolve_logs([], NOW - timedelta(hours=1), NOW, None)

    assert len(paths) == 1


def test_live_services_populates_every_service_except_log_volume():
    services = _live_services(TailCWConfig(), _make_session())
    unset = {field.name for field in fields(ShellServices) if getattr(services, field.name) is None}

    assert unset == {'log_volume'}


def test_live_services_are_callable_without_touching_aws():
    services = _live_services(TailCWConfig(), _make_session())
    populated = [field.name for field in fields(ShellServices) if getattr(services, field.name) is not None]

    assert all(callable(getattr(services, name)) for name in populated)


def test_live_services_fetch_metrics_threads_session_credentials():
    session = _make_session()
    services = _live_services(TailCWConfig(), session)
    recorded: list[dict[str, object]] = []

    def fake_fetch(queries, start_time, end_time, **kwargs):
        recorded.append({'queries': queries, 'start': start_time, 'end': end_time, **kwargs})
        return []

    assert services.fetch_metrics is not None
    with patch('tail_cw.__main__.fetch_metric_data', fake_fetch):
        assert services.fetch_metrics([{'Id': 'm1'}], session.start, session.end) == []

    assert recorded[0]['profile_name'] == 'dev'
    assert recorded[0]['region_name'] == 'us-west-2'


def test_live_services_count_events_caps_the_scan():
    session = _make_session()
    services = _live_services(TailCWConfig(), session)
    events = [
        LogEvent(
            log_group='/g',
            log_stream='s',
            timestamp=NOW,
            message='m',
            event_id=f'event-{index}',
            ingestion_time=None,
        )
        for index in range(3)
    ]

    assert services.count_events is not None
    with patch('tail_cw.__main__.fetch_log_events', lambda *_args, **_kwargs: iter(events)):
        assert services.count_events('/g', session.start, session.end) == 3


def test_live_services_resolve_logs_builds_one_request_per_group(tmp_path):
    session = _make_session()
    config = TailCWConfig()
    config.cache.cache_dir = tmp_path / 'cache'
    services = _live_services(config, session)
    recorded: list[Sequence[FetchRequest]] = []

    def fake_resolve(requests, _config):
        recorded.append(requests)
        return []

    assert services.resolve_logs is not None
    with patch('tail_cw.__main__.resolve_parquet_paths', fake_resolve):
        services.resolve_logs(['/one', '/two'], session.start, session.end, 'ERROR')

    requests = recorded[0]
    assert [request.log_group for request in requests] == ['/one', '/two']
    assert {request.filter_pattern for request in requests} == {'ERROR'}
    assert {request.profile for request in requests} == {'dev'}


def _run_shell_without_a_terminal(
    monkeypatch,
    config: TailCWConfig,
    session: Session,
    seed: ShellSeed,
) -> TailCWApp:
    started: list[TailCWApp] = []

    def fake_run(app: TailCWApp) -> None:
        started.append(app)

    monkeypatch.setattr(TailCWApp, 'run', fake_run)
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
