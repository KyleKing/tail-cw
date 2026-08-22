# ruff: file-ignore[unused-async] - the AWS fakes are async generators or awaitable stand-ins
"""Unit tests for the CLI module (time parsing, arg parsing, export pipelines)."""

import contextlib
import io
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tail_cw.aws.alarms import AlarmSummary, AlarmTransition
from tail_cw.aws.dashboards import Dashboard, DashboardSummary, TextWidget, WidgetLayout
from tail_cw.aws.events import LogEvent
from tail_cw.aws.insights import InsightsQueryError, InsightsResult
from tail_cw.aws.log_groups import LogGroupInfo
from tail_cw.aws.metrics import MetricDefinition, MetricSeries
from tail_cw.cache.storage import read_parquet_to_log_events
from tail_cw.cli import (
    FetchRequest,
    Session,
    ShellSeed,
    TailRequest,
    expand_presets,
    iter_tail_events,
    parse_time,
    resolve_parquet_path,
    resolve_parquet_paths,
    run_cli,
    seed_from_args,
    session_from_args,
    stream_ndjson,
    write_ndjson,
)
from tail_cw.config import CacheConfig, TailCWConfig
from tail_cw.parser import build_parser

NOW = datetime(2026, 7, 5, 12, 0, 0, tzinfo=UTC)


def _make_events(count: int = 3, *, log_group: str = '/aws/test/group') -> list[LogEvent]:
    return [
        LogEvent(
            log_group=log_group,
            log_stream='stream-1',
            timestamp=NOW - timedelta(minutes=count - index),
            message=f'{{"level":"INFO","index":{index}}}',
            ingestion_time=None,
        )
        for index in range(count)
    ]


_CLIENT = object()
"""Stand-in client: every fake fetcher ignores it, and no real call is made."""


async def _collect(events: AsyncIterator[LogEvent]) -> list[LogEvent]:
    return [event async for event in events]


def _async_iter_factory(items: list[Any]) -> Callable[..., AsyncIterator[Any]]:
    """Build a replacement for an async-generator AWS call that yields `items`."""

    async def factory(*_args: Any, **_kwargs: Any) -> AsyncIterator[Any]:
        for item in items:
            yield item

    return factory


def _async_value_factory(value: Any) -> Callable[..., Any]:
    """Build a replacement for a coroutine AWS call that returns `value`."""

    async def factory(*_args: Any, **_kwargs: Any) -> Any:
        return value

    return factory


class _FakeFetcher:
    def __init__(self, events: list[LogEvent]) -> None:
        self.events = events
        self.calls: list[dict[str, object]] = []

    async def __call__(self, _client, log_group, start_time, end_time, **kwargs) -> AsyncIterator[LogEvent]:
        self.calls.append({'log_group': log_group, 'start_time': start_time, 'end_time': end_time, **kwargs})
        for event in self.events:
            yield event


class _GroupFetcher:
    """Fetcher returning events only for the log groups it was told about."""

    def __init__(self, populated: set[str]) -> None:
        self.populated = populated
        self.calls: list[str] = []

    async def __call__(self, _client, log_group, start_time, end_time, **kwargs) -> AsyncIterator[LogEvent]:
        del start_time, end_time, kwargs
        self.calls.append(log_group)
        if log_group not in self.populated:
            return
        for event in _make_events(2, log_group=log_group):
            yield event


class _FakeStreamer:
    def __init__(self, events: list[LogEvent]) -> None:
        self.events = events
        self.calls: list[dict[str, object]] = []

    async def __call__(self, _client, log_groups, **kwargs) -> AsyncIterator[LogEvent]:
        self.calls.append({'log_groups': log_groups, **kwargs})
        for event in self.events:
            yield event


class _FakePool:
    """Stands in for a ClientPool without opening any AWS client."""

    def __init__(self) -> None:
        self.requested: list[str] = []
        self.opened_with: dict[str, object] = {}

    async def client(self, service_name: str) -> str:
        self.requested.append(service_name)
        return f'client:{service_name}'


@contextlib.asynccontextmanager
async def _fake_client_pool(**kwargs: object) -> AsyncIterator[_FakePool]:
    """Replacement for `client_pool` that records its credentials and opens nothing.

    Yields:
        The fake pool.
    """
    pool = _FakePool()
    pool.opened_with = dict(kwargs)
    yield pool


class _RecordingShell:
    def __init__(self) -> None:
        self.calls: list[tuple[TailCWConfig, Session, ShellSeed]] = []

    def __call__(self, config, session, seed) -> None:
        self.calls.append((config, session, seed))


def _make_config(tmp_path: Path) -> TailCWConfig:
    return TailCWConfig(cache=CacheConfig(cache_dir=tmp_path / 'cache'))


def _make_request(
    log_group: str = '/aws/test/group',
    *,
    profile: str | None = None,
    region: str | None = None,
) -> FetchRequest:
    """A request over one aligned five-minute segment, well behind the ingestion window."""
    return FetchRequest(
        log_group=log_group,
        start_time=NOW - timedelta(hours=1),
        end_time=NOW - timedelta(minutes=55),
        profile=profile,
        region=region,
    )


def _write_config_file(tmp_path: Path) -> Path:
    config_path = tmp_path / 'config.toml'
    cache_dir = tmp_path / 'cache'
    config_path.write_text(f'[cache]\ncache_dir = "{cache_dir.as_posix()}"\n', encoding='utf-8')
    return config_path


def _make_dashboard(name: str = 'prod-overview') -> Dashboard:
    return Dashboard(
        name=name,
        widgets=[TextWidget(layout=WidgetLayout(x=0, y=0, width=6, height=3), markdown='hello')],
    )


def _make_group(name: str) -> LogGroupInfo:
    return LogGroupInfo(name=name, arn=f'arn:{name}', stored_bytes=10, retention_days=7, created=NOW)


@pytest.mark.parametrize(
    ('value', 'expected_delta'),
    [
        ('15m', timedelta(minutes=15)),
        ('2h', timedelta(hours=2)),
        ('3d', timedelta(days=3)),
        (' 30m ', timedelta(minutes=30)),
    ],
)
def test_parse_time_relative(value, expected_delta):
    assert parse_time(value, now=NOW) == NOW - expected_delta


def test_parse_time_absolute_with_timezone():
    assert parse_time('2026-07-05T10:30:00+00:00', now=NOW) == datetime(2026, 7, 5, 10, 30, tzinfo=UTC)


def test_parse_time_absolute_naive_assumes_utc():
    assert parse_time('2026-07-05 10:30:00', now=NOW) == datetime(2026, 7, 5, 10, 30, tzinfo=UTC)


@pytest.mark.parametrize('value', ['15x', 'soon', '', '1.5h', '-2h', 'h', '2 h'])
def test_parse_time_invalid(value):
    with pytest.raises(ValueError, match='Invalid time'):
        parse_time(value, now=NOW)


def test_build_parser_no_subcommand():
    args = build_parser().parse_args([])

    assert args.command is None
    assert args.profile is None
    assert args.region is None
    assert args.config_path is None


def test_build_parser_logs_defaults():
    args = build_parser().parse_args(['logs', '/aws/lambda/fn'])

    assert args.command == 'logs'
    assert args.patterns == ['/aws/lambda/fn']
    assert args.start == '1h'
    assert args.end is None
    assert args.filter_pattern is None
    assert args.no_cache is False


def test_build_parser_logs_all_flags(tmp_path):
    args = build_parser().parse_args(
        [
            'logs',
            '/group/one',
            '/group/two',
            '--start',
            '30m',
            '--end',
            '2026-07-05T12:00:00+00:00',
            '--filter',
            'ERROR',
            '--profile',
            'dev',
            '--region',
            'us-west-2',
            '--config',
            str(tmp_path / 'config.toml'),
            '--no-cache',
        ]
    )

    assert args.patterns == ['/group/one', '/group/two']
    assert args.start == '30m'
    assert args.end == '2026-07-05T12:00:00+00:00'
    assert args.filter_pattern == 'ERROR'
    assert args.profile == 'dev'
    assert args.region == 'us-west-2'
    assert args.config_path == tmp_path / 'config.toml'
    assert args.no_cache is True


def test_build_parser_tail_accepts_no_patterns():
    args = build_parser().parse_args(['tail'])

    assert args.command == 'tail'
    assert args.patterns == []
    assert args.start == '1h'


def test_build_parser_dash_defaults():
    args = build_parser().parse_args(['dash'])

    assert args.command == 'dash'
    assert args.name is None
    assert args.demo is False
    assert args.start == '3h'


def test_build_parser_dash_named_with_demo():
    args = build_parser().parse_args(['dash', 'prod-overview', '--demo'])

    assert args.name == 'prod-overview'
    assert args.demo is True


def test_build_parser_export_logs_defaults():
    args = build_parser().parse_args(['export', 'logs', '/aws/lambda/fn'])

    assert args.command == 'export'
    assert args.export_command == 'logs'
    assert args.log_group == '/aws/lambda/fn'
    assert args.start == '1h'
    assert args.no_cache is False


def test_build_parser_export_tail_defaults():
    args = build_parser().parse_args(['export', 'tail', '/group/one', '/group/two'])

    assert args.export_command == 'tail'
    assert args.log_groups == ['/group/one', '/group/two']
    assert args.backfill is None
    assert args.filter_pattern is None


def test_build_parser_export_groups_pattern_optional():
    assert build_parser().parse_args(['export', 'groups']).pattern is None
    assert build_parser().parse_args(['export', 'groups', '/aws/lambda/*']).pattern == '/aws/lambda/*'


def test_build_parser_export_dashboard_flags(tmp_path):
    args = build_parser().parse_args(['export', 'dashboard', '--file', str(tmp_path / 'dash.json'), '--demo'])

    assert args.name is None
    assert args.dashboard_file == tmp_path / 'dash.json'
    assert args.demo is True


@pytest.mark.parametrize(
    'argv',
    [
        ['logs', '/g', '--json'],
        ['tail', '/g', '--json'],
        ['dash', 'name', '--json'],
        ['export', 'logs', '/g', '--json'],
    ],
)
def test_build_parser_rejects_json_flag(argv):
    with pytest.raises(SystemExit):
        build_parser().parse_args(argv)


@pytest.mark.parametrize(
    ('argv', 'expected_view', 'expected_targets'),
    [
        (['logs', '/group/one'], 'logs', ('/group/one',)),
        (['logs', '/group/one', '/group/two'], 'logs', ('/group/one', '/group/two')),
        (['logs'], 'groups', ()),
        (['tail', '/group/one'], 'tail', ('/group/one',)),
        (['tail'], 'groups', ()),
        (['dash', 'prod-overview'], 'dashboard', ('prod-overview',)),
        (['dash'], 'dashboards', ()),
        ([], 'groups', ()),
    ],
)
def test_seed_from_args(argv, expected_view, expected_targets):
    seed = seed_from_args(build_parser().parse_args(argv))

    assert seed.view == expected_view
    assert seed.targets == expected_targets
    assert seed.demo is False


def test_seed_from_args_dash_demo():
    seed = seed_from_args(build_parser().parse_args(['dash', '--demo']))

    assert seed.view == 'dashboard'
    assert seed.targets == ('demo',)
    assert seed.demo is True


def test_seed_from_args_dash_demo_ignores_name():
    seed = seed_from_args(build_parser().parse_args(['dash', 'prod-overview', '--demo']))

    assert seed.targets == ('demo',)
    assert seed.demo is True


def test_expand_presets_passes_plain_patterns_through():
    assert expand_presets(['/group/one', '/aws/*'], {}) == ['/group/one', '/aws/*']


def test_expand_presets_substitutes_the_named_groups():
    presets = {'api': ['/aws/lambda/api-a', '/ecs/api-b']}

    assert expand_presets(['@api', '/ecs/web'], presets) == ['/aws/lambda/api-a', '/ecs/api-b', '/ecs/web']


def test_expand_presets_rejects_an_unknown_name():
    with pytest.raises(ValueError, match="Unknown preset '@web'"):
        expand_presets(['@web'], {'api': ['/a']})


def test_expand_presets_names_the_configured_presets_in_the_error():
    with pytest.raises(ValueError, match='@api, @web'):
        expand_presets(['@nope'], {'web': ['/w'], 'api': ['/a']})


def test_expand_presets_rejects_a_bare_at_sign():
    with pytest.raises(ValueError, match="Unknown preset '@'"):
        expand_presets(['@'], {'api': ['/a']})


def test_expand_presets_rejects_an_empty_preset():
    with pytest.raises(ValueError, match="Preset '@api' lists no log groups"):
        expand_presets(['@api'], {'api': []})


def test_seed_from_args_expands_a_preset():
    args = build_parser().parse_args(['tail', '@api'])

    seed = seed_from_args(args, {'api': ['/aws/lambda/api-a', '/ecs/api-b']})

    assert seed == ShellSeed(view='tail', targets=('/aws/lambda/api-a', '/ecs/api-b'))


def test_seed_from_args_rejects_an_unknown_preset():
    args = build_parser().parse_args(['logs', '@api'])

    with pytest.raises(ValueError, match='Unknown preset'):
        seed_from_args(args)


def _write_preset_config(tmp_path: Path) -> Path:
    config_path = tmp_path / 'config.toml'
    cache_dir = tmp_path / 'cache'
    config_path.write_text(
        f'[cache]\ncache_dir = "{cache_dir.as_posix()}"\n\n[presets]\napi = ["/aws/lambda/api-a", "/ecs/api-b"]\n',
        encoding='utf-8',
    )
    return config_path


def test_run_cli_expands_a_preset_into_the_seed(tmp_path):
    config_path = _write_preset_config(tmp_path)
    shell = _RecordingShell()

    result = run_cli(['tail', '@api', '--config', str(config_path)], shell, is_tty=False)

    assert result == 0
    assert shell.calls[0][2] == ShellSeed(view='tail', targets=('/aws/lambda/api-a', '/ecs/api-b'))


def test_run_cli_reports_an_unknown_preset(tmp_path, capsys):
    config_path = _write_preset_config(tmp_path)
    shell = _RecordingShell()

    result = run_cli(['logs', '@web', '--config', str(config_path)], shell, is_tty=False)

    assert result == 2
    assert 'Unknown preset' in capsys.readouterr().err
    assert shell.calls == []


def test_session_from_args_builds_window():
    args = build_parser().parse_args(['logs', '/g', '--start', '30m'])

    session = session_from_args(args, NOW)

    assert session.start == NOW - timedelta(minutes=30)
    assert session.end == NOW
    assert session.window_label().endswith('UTC')


def test_session_from_args_threads_filter_and_credentials():
    args = build_parser().parse_args(
        ['logs', '/g', '--filter', 'ERROR', '--profile', 'dev', '--region', 'us-west-2'],
    )

    session = session_from_args(args, NOW)

    assert session.filter_pattern == 'ERROR'
    assert session.profile == 'dev'
    assert session.region == 'us-west-2'
    assert session.selected_groups == []


def test_session_from_args_explicit_end():
    args = build_parser().parse_args(['logs', '/g', '--start', '3h', '--end', '1h'])

    session = session_from_args(args, NOW)

    assert session.start == NOW - timedelta(hours=3)
    assert session.end == NOW - timedelta(hours=1)


@pytest.mark.parametrize(('start', 'end'), [('1h', '2h'), ('1h', '1h')])
def test_session_from_args_rejects_start_at_or_after_end(start, end):
    args = build_parser().parse_args(['logs', '/g', '--start', start, '--end', end])

    with pytest.raises(ValueError, match='must be before'):
        session_from_args(args, NOW)


async def test_resolve_parquet_path_fetches_on_miss(tmp_path):
    fetcher = _FakeFetcher(_make_events())

    paths = await resolve_parquet_path(_CLIENT, _make_request(), _make_config(tmp_path), fetch_events=fetcher)

    assert [path.exists() for path in paths] == [True]
    assert len(fetcher.calls) == 1


async def test_resolve_parquet_path_uses_cache_on_hit(tmp_path):
    config = _make_config(tmp_path)
    request = _make_request()
    first_path = await resolve_parquet_path(_CLIENT, request, config, fetch_events=_FakeFetcher(_make_events()))

    second_fetcher = _FakeFetcher(_make_events())
    second_path = await resolve_parquet_path(_CLIENT, request, config, fetch_events=second_fetcher)

    assert second_path == first_path
    assert second_fetcher.calls == []


async def test_resolve_parquet_path_no_cache_refetches(tmp_path):
    config = _make_config(tmp_path)
    request = _make_request()
    await resolve_parquet_path(_CLIENT, request, config, fetch_events=_FakeFetcher(_make_events()))

    refetcher = _FakeFetcher(_make_events())
    paths = await resolve_parquet_path(_CLIENT, request, config, use_cache=False, fetch_events=refetcher)

    assert paths
    assert len(refetcher.calls) == 1


async def test_resolve_parquet_path_empty_fetch_returns_nothing(tmp_path):
    fetcher = _FakeFetcher([])

    assert await resolve_parquet_path(_CLIENT, _make_request(), _make_config(tmp_path), fetch_events=fetcher) == []


async def test_resolve_parquet_path_threads_fetch_parameters(tmp_path):
    fetcher = _FakeFetcher(_make_events())
    request = _make_request(profile='dev', region='us-west-2')

    await resolve_parquet_path(_CLIENT, request, _make_config(tmp_path), fetch_events=fetcher)

    call = fetcher.calls[0]
    assert call['log_group'] == '/aws/test/group'
    assert (call['start_time'], call['end_time']) == (request.start_time, request.end_time)
    # No server-side filter: the whole window is cached once and filtered on read.
    assert 'filter_pattern' not in call
    # Profile and region reach the pool that built the client, and the cache key, not the fetch itself
    assert 'profile_name' not in call


async def test_resolve_parquet_path_profile_changes_cache_entry(tmp_path):
    config = _make_config(tmp_path)
    request = _make_request()
    await resolve_parquet_path(_CLIENT, request, config, fetch_events=_FakeFetcher(_make_events()))

    profiled_fetcher = _FakeFetcher(_make_events())
    await resolve_parquet_path(_CLIENT, _make_request(profile='dev'), config, fetch_events=profiled_fetcher)

    assert len(profiled_fetcher.calls) == 1


async def test_resolve_parquet_paths_empty_request_list(tmp_path):
    assert await resolve_parquet_paths(_CLIENT, [], _make_config(tmp_path)) == []


async def test_resolve_parquet_paths_single_request(tmp_path):
    fetcher = _FakeFetcher(_make_events())

    paths = await resolve_parquet_paths(_CLIENT, [_make_request()], _make_config(tmp_path), fetch_events=fetcher)

    assert [path.exists() for path in paths] == [True]
    assert len(fetcher.calls) == 1


async def test_resolve_parquet_paths_single_request_without_events(tmp_path):
    paths = await resolve_parquet_paths(
        _CLIENT, [_make_request()], _make_config(tmp_path), fetch_events=_FakeFetcher([])
    )

    assert paths == []


def _first_group(path: Path) -> str:
    return next(iter(read_parquet_to_log_events(path))).log_group


async def test_resolve_parquet_paths_keeps_request_order(tmp_path):
    groups = [f'/group/{index}' for index in range(4)]
    fetcher = _GroupFetcher(set(groups))
    requests = [_make_request(group) for group in groups]

    paths = await resolve_parquet_paths(_CLIENT, requests, _make_config(tmp_path), fetch_events=fetcher)

    assert [_first_group(path) for path in paths] == groups


async def test_resolve_parquet_paths_drops_empty_results(tmp_path):
    groups = ['/group/a', '/group/b', '/group/c']
    fetcher = _GroupFetcher({'/group/a', '/group/c'})
    requests = [_make_request(group) for group in groups]

    paths = await resolve_parquet_paths(_CLIENT, requests, _make_config(tmp_path), fetch_events=fetcher)

    assert [_first_group(path) for path in paths] == ['/group/a', '/group/c']
    assert sorted(fetcher.calls) == groups


async def test_resolve_parquet_paths_parallel_fanout_keeps_every_group(tmp_path):
    """A parallel fan-out keeps every group: workers share one cache instance."""
    groups = [f'/group/{index}' for index in range(6)]
    fetcher = _GroupFetcher(set(groups))
    requests = [_make_request(group) for group in groups]

    paths = await resolve_parquet_paths(_CLIENT, requests, _make_config(tmp_path), fetch_events=fetcher)

    assert [_first_group(path) for path in paths] == groups


def test_write_ndjson():
    stream = io.StringIO()

    count = write_ndjson(_make_events(2), stream)

    assert count == 2
    lines = stream.getvalue().strip().splitlines()
    assert len(lines) == 2
    record = json.loads(lines[0])
    assert record['log_group'] == '/aws/test/group'
    assert record['log_stream'] == 'stream-1'
    assert record['message'] == '{"level":"INFO","index":0}'
    assert datetime.fromisoformat(record['timestamp']) == NOW - timedelta(minutes=2)


async def test_stream_ndjson_flushes_per_line():
    class _FlushCountingStream(io.StringIO):
        def __init__(self) -> None:
            super().__init__()
            self.flush_count = 0

        def flush(self) -> None:
            self.flush_count += 1
            super().flush()

    stream = _FlushCountingStream()

    count = await stream_ndjson(_async_iter_factory(_make_events(3))(), stream)

    assert count == 3
    assert stream.flush_count == 3
    assert len(stream.getvalue().strip().splitlines()) == 3


async def test_iter_tail_events_without_backfill_skips_fetch():
    fetcher = _FakeFetcher(_make_events(2))
    streamer = _FakeStreamer(_make_events(1))
    request = TailRequest(log_groups=('/aws/test/group',))

    events = await _collect(iter_tail_events(_CLIENT, request, now=NOW, fetch_events=fetcher, stream_events=streamer))

    assert len(events) == 1
    assert fetcher.calls == []


async def test_iter_tail_events_backfills_each_group():
    fetcher = _FakeFetcher(_make_events(1))
    streamer = _FakeStreamer([])
    request = TailRequest(
        log_groups=('/group/one', '/group/two'),
        backfill_start=NOW - timedelta(minutes=5),
        filter_pattern='ERROR',
    )

    events = await _collect(iter_tail_events(_CLIENT, request, now=NOW, fetch_events=fetcher, stream_events=streamer))

    assert len(events) == 2
    assert [call['log_group'] for call in fetcher.calls] == ['/group/one', '/group/two']
    assert all(call['filter_pattern'] == 'ERROR' for call in fetcher.calls)


def test_run_cli_bare_on_tty_opens_shell(tmp_path):
    config_path = _write_config_file(tmp_path)
    shell = _RecordingShell()

    result = run_cli(['--config', str(config_path)], shell, is_tty=True)

    assert result == 0
    assert len(shell.calls) == 1
    _config, _session, seed = shell.calls[0]
    assert seed == ShellSeed(view='groups')


def test_run_cli_bare_without_tty_prints_help(capsys):
    shell = _RecordingShell()

    result = run_cli([], shell, is_tty=False)

    assert result == 2
    assert 'usage: tail-cw' in capsys.readouterr().err
    assert shell.calls == []


def test_run_cli_logs_seeds_shell(tmp_path):
    config_path = _write_config_file(tmp_path)
    shell = _RecordingShell()

    result = run_cli(
        ['logs', '/group/one', '/group/two', '--filter', 'ERROR', '--profile', 'dev', '--config', str(config_path)],
        shell,
        is_tty=False,
    )

    assert result == 0
    config, session, seed = shell.calls[0]
    assert config.cache.cache_dir == tmp_path / 'cache'
    assert session.filter_pattern == 'ERROR'
    assert session.profile == 'dev'
    assert seed == ShellSeed(view='logs', targets=('/group/one', '/group/two'))


def test_run_cli_tail_seeds_tail_view(tmp_path):
    config_path = _write_config_file(tmp_path)
    shell = _RecordingShell()

    result = run_cli(['tail', '/group/one', '--config', str(config_path)], shell, is_tty=False)

    assert result == 0
    assert shell.calls[0][2] == ShellSeed(view='tail', targets=('/group/one',))


def test_run_cli_dash_demo_seeds_demo_dashboard(tmp_path):
    config_path = _write_config_file(tmp_path)
    shell = _RecordingShell()

    result = run_cli(['dash', '--demo', '--config', str(config_path)], shell, is_tty=False)

    assert result == 0
    seed = shell.calls[0][2]
    assert seed.view == 'dashboard'
    assert seed.demo is True


def test_run_cli_dash_window_defaults_to_three_hours(tmp_path):
    config_path = _write_config_file(tmp_path)
    shell = _RecordingShell()

    run_cli(['dash', 'prod-overview', '--config', str(config_path)], shell, is_tty=False)

    _config, session, _seed = shell.calls[0]
    assert session.end - session.start == timedelta(hours=3)


def test_run_cli_requires_run_shell(tmp_path, capsys):
    config_path = _write_config_file(tmp_path)

    result = run_cli(['logs', '/g', '--config', str(config_path)], None, is_tty=False)

    assert result == 1
    assert 'unavailable' in capsys.readouterr().err


def test_run_cli_bare_on_tty_requires_run_shell(capsys):
    result = run_cli([], None, is_tty=True)

    assert result == 1
    assert 'unavailable' in capsys.readouterr().err


def test_run_cli_invalid_start(capsys):
    result = run_cli(['logs', '/g', '--start', 'bogus'], _RecordingShell(), is_tty=False)

    assert result == 2
    assert 'Invalid time' in capsys.readouterr().err


def test_run_cli_start_after_end(capsys):
    result = run_cli(['logs', '/g', '--start', '1h', '--end', '2h'], _RecordingShell(), is_tty=False)

    assert result == 2
    assert 'must be before' in capsys.readouterr().err


def test_run_cli_bad_config(tmp_path, capsys):
    config_path = tmp_path / 'config.toml'
    config_path.write_text('not valid toml [', encoding='utf-8')

    result = run_cli(['logs', '/g', '--config', str(config_path)], _RecordingShell(), is_tty=False)

    assert result == 1
    assert 'Configuration error' in capsys.readouterr().err


def test_run_cli_export_without_subcommand_prints_help(capsys):
    result = run_cli(['export'], _RecordingShell(), is_tty=True)

    assert result == 2
    assert 'usage: tail-cw' in capsys.readouterr().err


def test_run_cli_export_logs_writes_ndjson(tmp_path, capsys):
    config_path = _write_config_file(tmp_path)

    result = run_cli(
        ['export', 'logs', '/aws/test/group', '--start', '2m', '--config', str(config_path)],
        _RecordingShell(),
        fetch_events=_FakeFetcher(_make_events(3)),
        is_tty=False,
    )

    assert result == 0
    lines = capsys.readouterr().out.strip().splitlines()
    records = [json.loads(line) for line in lines]
    assert [record['message'] for record in records] == [f'{{"level":"INFO","index":{index}}}' for index in range(3)]


def test_run_cli_export_logs_no_cache_refetches(tmp_path, capsys):
    config_path = _write_config_file(tmp_path)
    argv = ['export', 'logs', '/aws/test/group', '--start', '2m', '--config', str(config_path)]
    run_cli(argv, None, fetch_events=_FakeFetcher(_make_events(2)), is_tty=False)
    capsys.readouterr()

    refetcher = _FakeFetcher(_make_events(2))
    result = run_cli([*argv, '--no-cache'], None, fetch_events=refetcher, is_tty=False)

    assert result == 0
    assert len(refetcher.calls) == 1


def test_run_cli_export_logs_no_events(tmp_path, capsys):
    config_path = _write_config_file(tmp_path)

    result = run_cli(
        ['export', 'logs', '/aws/test/group', '--start', '2m', '--config', str(config_path)],
        None,
        fetch_events=_FakeFetcher([]),
        is_tty=False,
    )

    assert result == 0
    captured = capsys.readouterr()
    assert not captured.out
    assert 'No events found' in captured.err


def test_run_cli_export_logs_invalid_window(capsys):
    result = run_cli(['export', 'logs', '/g', '--start', '1h', '--end', '2h'], None, is_tty=False)

    assert result == 2
    assert 'must be before' in capsys.readouterr().err


def test_run_cli_export_logs_bad_config(tmp_path, capsys):
    config_path = tmp_path / 'config.toml'
    config_path.write_text('not valid toml [', encoding='utf-8')

    result = run_cli(['export', 'logs', '/g', '--config', str(config_path)], None, is_tty=False)

    assert result == 1
    assert 'Configuration error' in capsys.readouterr().err


def test_run_cli_export_tail_streams_ndjson(tmp_path, capsys):
    config_path = _write_config_file(tmp_path)
    streamer = _FakeStreamer(_make_events(3))

    result = run_cli(
        ['export', 'tail', '/aws/test/group', '--filter', 'ERROR', '--config', str(config_path)],
        None,
        stream_events=streamer,
        is_tty=False,
    )

    assert result == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 3
    call = streamer.calls[0]
    assert call['log_groups'] == ('/aws/test/group',)
    assert call['filter_pattern'] == 'ERROR'


def test_run_cli_export_tail_backfill_before_live(tmp_path, capsys):
    config_path = _write_config_file(tmp_path)
    live_events = [
        LogEvent(
            log_group='/aws/test/group',
            log_stream='stream-live',
            timestamp=NOW,
            message='live',
            ingestion_time=None,
        ),
    ]
    fetcher = _FakeFetcher(_make_events(2))

    result = run_cli(
        ['export', 'tail', '/aws/test/group', '--backfill', '15m', '--config', str(config_path)],
        None,
        fetch_events=fetcher,
        stream_events=_FakeStreamer(live_events),
        is_tty=False,
    )

    assert result == 0
    records = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert [record['message'] for record in records] == [
        '{"level":"INFO","index":0}',
        '{"level":"INFO","index":1}',
        'live',
    ]
    fetch_call = fetcher.calls[0]
    start_time, end_time = fetch_call['start_time'], fetch_call['end_time']
    assert isinstance(start_time, datetime)
    assert isinstance(end_time, datetime)
    assert end_time - start_time == timedelta(minutes=15)


def test_run_cli_export_tail_rejects_more_than_ten_groups(capsys):
    groups = [f'/group/{index}' for index in range(11)]

    result = run_cli(['export', 'tail', *groups], None, is_tty=False)

    assert result == 2
    assert 'At most 10 log groups' in capsys.readouterr().err


def test_run_cli_export_tail_invalid_backfill(capsys):
    result = run_cli(['export', 'tail', '/g', '--backfill', 'bogus'], None, is_tty=False)

    assert result == 2
    assert 'Invalid time' in capsys.readouterr().err


def test_run_cli_export_tail_future_backfill(capsys):
    result = run_cli(['export', 'tail', '/g', '--backfill', '2999-01-01T00:00:00+00:00'], None, is_tty=False)

    assert result == 2
    assert 'must be in the past' in capsys.readouterr().err


def test_run_cli_export_tail_bad_config(tmp_path, capsys):
    config_path = tmp_path / 'config.toml'
    config_path.write_text('not valid toml [', encoding='utf-8')

    result = run_cli(['export', 'tail', '/g', '--config', str(config_path)], None, is_tty=False)

    assert result == 1
    assert 'Configuration error' in capsys.readouterr().err


def test_run_cli_export_tail_keyboard_interrupt_exits_cleanly(tmp_path, capsys):
    config_path = _write_config_file(tmp_path)

    async def interrupted_stream(_client, log_groups, **kwargs) -> AsyncIterator[LogEvent]:
        del log_groups, kwargs
        for event in _make_events(1):
            yield event
        raise KeyboardInterrupt

    result = run_cli(
        ['export', 'tail', '/aws/test/group', '--config', str(config_path)],
        None,
        stream_events=interrupted_stream,
        is_tty=False,
    )

    assert result == 0
    assert len(capsys.readouterr().out.strip().splitlines()) == 1


def test_run_cli_export_groups_writes_every_group(tmp_path, capsys, monkeypatch):
    config_path = _write_config_file(tmp_path)
    groups = [_make_group('/aws/lambda/api'), _make_group('/ecs/web')]
    monkeypatch.setattr('tail_cw.cli.client_pool', _fake_client_pool)
    monkeypatch.setattr('tail_cw.cli.describe_log_groups', _async_iter_factory(groups))

    result = run_cli(['export', 'groups', '--config', str(config_path)], None, is_tty=False)

    assert result == 0
    records = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert [record['name'] for record in records] == ['/aws/lambda/api', '/ecs/web']
    assert records[0]['retention_days'] == 7
    assert records[0]['created'] == NOW.isoformat()


def test_run_cli_export_groups_filters_by_pattern(tmp_path, capsys, monkeypatch):
    config_path = _write_config_file(tmp_path)
    groups = [_make_group('/aws/lambda/api'), _make_group('/ecs/web')]
    monkeypatch.setattr('tail_cw.cli.client_pool', _fake_client_pool)
    monkeypatch.setattr('tail_cw.cli.describe_log_groups', _async_iter_factory(groups))

    result = run_cli(['export', 'groups', '/aws/lambda/*', '--config', str(config_path)], None, is_tty=False)

    assert result == 0
    records = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert [record['name'] for record in records] == ['/aws/lambda/api']


def test_run_cli_export_groups_bad_config(tmp_path, capsys):
    config_path = tmp_path / 'config.toml'
    config_path.write_text('not valid toml [', encoding='utf-8')

    result = run_cli(['export', 'groups', '--config', str(config_path)], None, is_tty=False)

    assert result == 1
    assert 'Configuration error' in capsys.readouterr().err


def test_run_cli_export_dashboards_writes_summaries(tmp_path, capsys, monkeypatch):
    config_path = _write_config_file(tmp_path)
    summaries = [
        DashboardSummary(name='prod-overview', arn='arn:one', size=120),
        DashboardSummary(name='api-latency', arn='arn:two', size=80),
    ]
    monkeypatch.setattr('tail_cw.cli.client_pool', _fake_client_pool)
    monkeypatch.setattr('tail_cw.cli.list_dashboards', _async_value_factory(summaries))

    result = run_cli(['export', 'dashboards', '--config', str(config_path)], None, is_tty=False)

    assert result == 0
    records = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert [record['name'] for record in records] == ['prod-overview', 'api-latency']
    assert records[0] == {'name': 'prod-overview', 'arn': 'arn:one', 'size': 120}


def test_run_cli_export_dashboards_threads_credentials(tmp_path, capsys, monkeypatch):
    """Credentials reach the client pool, which is now what resolves them."""
    config_path = _write_config_file(tmp_path)
    pools: list[_FakePool] = []

    @contextlib.asynccontextmanager
    async def _recording_pool(**kwargs: object) -> AsyncIterator[_FakePool]:
        pool = _FakePool()
        pool.opened_with = dict(kwargs)
        pools.append(pool)
        yield pool

    monkeypatch.setattr('tail_cw.cli.client_pool', _recording_pool)
    monkeypatch.setattr('tail_cw.cli.list_dashboards', _async_value_factory([]))

    result = run_cli(
        ['export', 'dashboards', '--profile', 'dev', '--region', 'us-west-2', '--config', str(config_path)],
        None,
        is_tty=False,
    )

    assert result == 0
    assert not capsys.readouterr().out
    assert pools[0].opened_with == {'profile_name': 'dev', 'region_name': 'us-west-2'}
    assert pools[0].requested == ['cloudwatch']


def test_run_cli_export_dashboards_bad_config(tmp_path, capsys):
    config_path = tmp_path / 'config.toml'
    config_path.write_text('not valid toml [', encoding='utf-8')

    result = run_cli(['export', 'dashboards', '--config', str(config_path)], None, is_tty=False)

    assert result == 1
    assert 'Configuration error' in capsys.readouterr().err


def test_run_cli_export_dashboard_by_name(tmp_path, capsys, monkeypatch):
    config_path = _write_config_file(tmp_path)

    async def _get(_client, name, **_kwargs) -> Dashboard:
        return _make_dashboard(name)

    monkeypatch.setattr('tail_cw.cli.client_pool', _fake_client_pool)
    monkeypatch.setattr('tail_cw.cli.get_dashboard', _get)

    result = run_cli(['export', 'dashboard', 'prod-overview', '--config', str(config_path)], None, is_tty=False)

    assert result == 0
    record = json.loads(capsys.readouterr().out)
    assert record['name'] == 'prod-overview'
    assert record['widgets'][0]['type'] == 'text'


def test_run_cli_export_dashboard_from_file(tmp_path, capsys):
    config_path = _write_config_file(tmp_path)
    dashboard_file = tmp_path / 'local-dash.json'
    body = {'widgets': [{'type': 'text', 'x': 0, 'y': 0, 'width': 6, 'height': 3, 'properties': {'markdown': 'hi'}}]}
    dashboard_file.write_text(json.dumps(body), encoding='utf-8')

    result = run_cli(
        ['export', 'dashboard', '--file', str(dashboard_file), '--config', str(config_path)],
        None,
        is_tty=False,
    )

    assert result == 0
    record = json.loads(capsys.readouterr().out)
    assert record['name'] == 'local-dash'
    assert record['widgets'][0]['markdown'] == 'hi'


def test_run_cli_export_dashboard_demo(tmp_path, capsys):
    config_path = _write_config_file(tmp_path)

    result = run_cli(['export', 'dashboard', '--demo', '--config', str(config_path)], None, is_tty=False)

    assert result == 0
    record = json.loads(capsys.readouterr().out)
    assert record['widgets']


def test_run_cli_export_dashboard_requires_a_source(capsys):
    result = run_cli(['export', 'dashboard'], None, is_tty=False)

    assert result == 2
    assert 'Provide a dashboard name, --file, or --demo' in capsys.readouterr().err


def test_run_cli_export_dashboard_reports_load_failure(tmp_path, capsys, monkeypatch):
    config_path = _write_config_file(tmp_path)

    async def _raise(_client, name, **_kwargs) -> Dashboard:
        msg = f'Dashboard {name} not found'
        raise ValueError(msg)

    monkeypatch.setattr('tail_cw.cli.client_pool', _fake_client_pool)
    monkeypatch.setattr('tail_cw.cli.get_dashboard', _raise)

    result = run_cli(['export', 'dashboard', 'missing', '--config', str(config_path)], None, is_tty=False)

    assert result == 1
    assert 'not found' in capsys.readouterr().err


def test_run_cli_export_dashboard_bad_config(tmp_path, capsys):
    config_path = tmp_path / 'config.toml'
    config_path.write_text('not valid toml [', encoding='utf-8')

    result = run_cli(['export', 'dashboard', '--demo', '--config', str(config_path)], None, is_tty=False)

    assert result == 1
    assert 'Configuration error' in capsys.readouterr().err


class _SeverityFetcher:
    """Yields one warning per group so a summary has something to roll up."""

    def __init__(self, messages: dict[str, list[str]]) -> None:
        self.messages = messages
        self.calls: list[str] = []

    async def __call__(self, _client, log_group, start_time, end_time, **kwargs) -> AsyncIterator[LogEvent]:
        del start_time, end_time, kwargs
        self.calls.append(log_group)
        for index, message in enumerate(self.messages.get(log_group, [])):
            yield LogEvent(
                log_group=log_group,
                log_stream='stream-1',
                timestamp=NOW - timedelta(minutes=index + 1),
                message=message,
                ingestion_time=None,
            )


def _summary_argv(tmp_path: Path, *extra: str) -> list[str]:
    # A window shorter than the smallest segment plans one fetch per group, so a
    # fake fetcher that ignores the window cannot double-count its events.
    return [
        'export',
        'summary',
        '/aws/lambda/*',
        '--start',
        '2m',
        '--config',
        str(_write_config_file(tmp_path)),
        *extra,
    ]


def _install_groups(
    monkeypatch,
    names: list[str],
    *,
    stored_bytes: int = 10,
    rates: dict[str, float] | None = None,
) -> None:
    groups = [replace(_make_group(name), stored_bytes=stored_bytes) for name in names]
    monkeypatch.setattr('tail_cw.cli.client_pool', _fake_client_pool)
    monkeypatch.setattr('tail_cw.cli.describe_log_groups', _async_iter_factory(groups))

    async def sample(_client, _names, **_kwargs):
        return dict(rates or {})

    monkeypatch.setattr('tail_cw.cli.measure_group_rates', sample)


def test_run_cli_export_summary_writes_markdown(tmp_path, capsys, monkeypatch):
    _install_groups(monkeypatch, ['/aws/lambda/one', '/aws/lambda/two', '/other'])
    fetcher = _SeverityFetcher(
        {
            '/aws/lambda/one': ['{"level":"warning","logger":"a","event":"disk nearly full"}'] * 2,
            '/aws/lambda/two': ['{"level":"info","logger":"a","event":"fine"}'],
        },
    )

    result = run_cli(_summary_argv(tmp_path), None, fetch_events=fetcher, is_tty=False)

    out = capsys.readouterr().out
    assert result == 0
    # The glob excluded /other, so it was never fetched.
    assert sorted(fetcher.calls) == ['/aws/lambda/one', '/aws/lambda/two']
    assert '# Warning-and-above patterns' in out
    assert 'warning a disk nearly full' in out
    assert 'fine' not in out


def test_run_cli_export_summary_writes_json(tmp_path, capsys, monkeypatch):
    _install_groups(monkeypatch, ['/aws/lambda/one'])
    fetcher = _SeverityFetcher({'/aws/lambda/one': ['{"level":"error","logger":"a","event":"boom"}']})

    result = run_cli(_summary_argv(tmp_path, '--format', 'json'), None, fetch_events=fetcher, is_tty=False)

    assert result == 0
    record = json.loads(capsys.readouterr().out)
    assert record['severity_totals'] == {'error': 1}
    assert record['patterns'][0]['count'] == 1
    assert record['patterns'][0]['log_groups'] == {'/aws/lambda/one': 1}


def test_run_cli_export_summary_names_the_groups_it_capped(tmp_path, capsys, monkeypatch):
    _install_groups(monkeypatch, ['/aws/lambda/one', '/aws/lambda/two'])
    fetcher = _SeverityFetcher({'/aws/lambda/one': ['{"level":"warning","logger":"a","event":"x"}']})

    result = run_cli(_summary_argv(tmp_path, '--max-groups', '1'), None, fetch_events=fetcher, is_tty=False)

    assert result == 0
    assert fetcher.calls == ['/aws/lambda/one']
    assert 'not fetched: /aws/lambda/two' in capsys.readouterr().err


def test_run_cli_export_trace_writes_otlp_for_the_spans_it_found(tmp_path, capsys, monkeypatch):
    trace_id = '1-68a1f2c3-4d5e6f708192a3b4c5d6e7f8'
    _install_groups(monkeypatch, ['/aws/lambda/one', '/aws/lambda/two'])
    fetcher = _SeverityFetcher(
        {
            '/aws/lambda/one': [f'{{"trace_id":"{trace_id}","service":"api","event":"in","duration_ms":12}}'],
            '/aws/lambda/two': [f'{{"trace_id":"{trace_id}","service":"payments","level":"error","event":"boom"}}'],
        },
    )
    config = str(_write_config_file(tmp_path))
    argv = ['export', 'trace', trace_id, '/aws/lambda/*', '--start', '2m', '--config', config]

    result = run_cli(argv, None, fetch_events=fetcher, is_tty=False)

    captured = capsys.readouterr()
    assert result == 0
    document = json.loads(captured.out)
    services = {
        attribute['value']['stringValue']
        for resource in document['resourceSpans']
        for attribute in resource['resource']['attributes']
    }
    assert services == {'api', 'payments'}
    assert 'first error in payments' in captured.err


def test_run_cli_export_trace_says_so_when_the_trace_is_not_in_the_window(tmp_path, capsys, monkeypatch):
    _install_groups(monkeypatch, ['/aws/lambda/one'])
    fetcher = _SeverityFetcher({'/aws/lambda/one': ['{"trace_id":"other","event":"in"}']})
    argv = ['export', 'trace', 'missing-id', '--start', '2m', '--config', str(_write_config_file(tmp_path))]

    result = run_cli(argv, None, fetch_events=fetcher, is_tty=False)

    assert result == 1
    assert 'has no spans' in capsys.readouterr().err


def test_run_cli_export_insights_writes_rows_and_reports_scanned_volume(tmp_path, capsys, monkeypatch):
    _install_groups(monkeypatch, ['/aws/lambda/one', '/other'])
    captured: dict[str, object] = {}

    async def fake_query(_client, **kwargs):
        captured.update(kwargs)
        return InsightsResult(
            columns=('day', 'events'),
            rows=({'day': '2026-08-14', 'events': '7'},),
            records_matched=7,
            records_scanned=100,
            bytes_scanned=1_500_000_000,
        )

    monkeypatch.setattr('tail_cw.cli.run_insights_query', fake_query)
    argv = [
        'export',
        'insights',
        '/aws/lambda/*',
        '--config',
        str(_write_config_file(tmp_path)),
        '--query',
        'filter @message like /boom/ | stats count(*) by bin(1d)',
        '--format',
        'md',
    ]

    result = run_cli(argv, None, is_tty=False)

    captured_output = capsys.readouterr()
    assert result == 0
    assert captured['log_groups'] == ['/aws/lambda/one']
    assert captured['query'] == 'filter @message like /boom/ | stats count(*) by bin(1d)'
    assert '| day | events |' in captured_output.out
    assert '| 2026-08-14 | 7 |' in captured_output.out
    # Insights bills on bytes scanned, so the caller is always told.
    assert '1.500 GB scanned' in captured_output.err


def _insights_argv(tmp_path: Path, *extra: str) -> list[str]:
    return [
        'export',
        'insights',
        '/aws/lambda/*',
        '--config',
        str(_write_config_file(tmp_path)),
        '--query',
        'filter @message like /boom/',
        *extra,
    ]


def _unreachable_query(_client, **_kwargs):
    msg = 'the preflight should have stopped this query'
    raise AssertionError(msg)


@pytest.mark.parametrize(
    ('extra', 'expected_code', 'expected_err'),
    [
        ((), 1, 'Above the 1 GB ceiling'),
        (('--dry-run',), 0, 'Estimate ~'),
    ],
)
def test_run_cli_export_insights_stops_before_billing(
    tmp_path,
    capsys,
    monkeypatch,
    extra,
    expected_code,
    expected_err,
):
    # A week's retention holding 70 GB is 10 GB a day, so an hour of it is over the ceiling.
    _install_groups(monkeypatch, ['/aws/lambda/one'], stored_bytes=70 * 10**9)
    monkeypatch.setattr('tail_cw.cli.run_insights_query', _unreachable_query)

    result = run_cli(_insights_argv(tmp_path, '--start', '12h', *extra), None, is_tty=False)

    assert result == expected_code
    assert expected_err in capsys.readouterr().err


def test_a_measured_rate_beats_the_stored_bytes_average(tmp_path, capsys, monkeypatch):
    """A group storing almost nothing can still be logging 10 MB a second right now."""
    _install_groups(monkeypatch, ['/aws/lambda/one'], stored_bytes=10, rates={'/aws/lambda/one': 10_000_000.0})
    monkeypatch.setattr('tail_cw.cli.run_insights_query', _unreachable_query)

    result = run_cli(_insights_argv(tmp_path, '--start', '1h'), None, is_tty=False)

    err = capsys.readouterr().err
    assert result == 1
    assert 'Estimate ~36.000 GB' in err
    assert 'samples spread across the window' in err


def test_run_cli_export_insights_runs_over_the_ceiling_when_told_to(tmp_path, capsys, monkeypatch):
    _install_groups(monkeypatch, ['/aws/lambda/one'], stored_bytes=70 * 10**9)

    async def fake_query(_client, **_kwargs):
        return InsightsResult(columns=(), rows=(), records_matched=0, records_scanned=0, bytes_scanned=0)

    monkeypatch.setattr('tail_cw.cli.run_insights_query', fake_query)

    result = run_cli(_insights_argv(tmp_path, '--start', '12h', '--yes'), None, is_tty=False)

    assert result == 0
    assert 'Above the' not in capsys.readouterr().err


def test_run_cli_export_insights_reports_a_failed_query(tmp_path, capsys, monkeypatch):
    _install_groups(monkeypatch, ['/aws/lambda/one'])

    async def failing_query(_client, **_kwargs):
        raise InsightsQueryError('Insights query q-1 ended as Failed')

    monkeypatch.setattr('tail_cw.cli.run_insights_query', failing_query)
    argv = ['export', 'insights', '/aws/lambda/*', '--config', str(_write_config_file(tmp_path)), '--query', 'filter x']

    result = run_cli(argv, None, is_tty=False)

    assert result == 1
    assert 'ended as Failed' in capsys.readouterr().err


def _make_alarm(name: str = 'svc-high-memory', state: str = 'ALARM') -> AlarmSummary:
    return AlarmSummary(
        name=name,
        state=state,
        state_reason='Threshold Crossed',
        state_updated=NOW,
        description='memory over 85%',
        namespace='AWS/ECS',
        metric_name='MemoryUtilization',
        dimensions=(('ClusterName', 'c1'), ('ServiceName', 's1')),
        statistic='Average',
        comparison='GreaterThanOrEqualToThreshold',
        threshold=85.0,
        period_seconds=120,
        datapoints_to_alarm=2,
        evaluation_periods=3,
        actions_enabled=True,
    )


def test_run_cli_export_alarms_includes_transition_counts(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr('tail_cw.cli.client_pool', _fake_client_pool)
    monkeypatch.setattr('tail_cw.cli.describe_alarms', _async_iter_factory([_make_alarm()]))
    monkeypatch.setattr(
        'tail_cw.cli.describe_alarm_history',
        _async_iter_factory(
            [
                AlarmTransition(alarm_name='svc-high-memory', moment=NOW, summary='OK to ALARM'),
                AlarmTransition(alarm_name='svc-high-memory', moment=NOW, summary='ALARM to OK'),
            ],
        ),
    )
    argv = ['export', 'alarms', 'svc', '--history', '--config', str(_write_config_file(tmp_path))]

    result = run_cli(argv, None, is_tty=False)

    assert result == 0
    record = json.loads(capsys.readouterr().out)
    assert record['name'] == 'svc-high-memory'
    assert record['dimensions'] == {'ClusterName': 'c1', 'ServiceName': 's1'}
    assert record['threshold'] == pytest.approx(85.0)
    assert record['transitions'] == 2
    assert [item['summary'] for item in record['history']] == ['OK to ALARM', 'ALARM to OK']


def test_run_cli_export_alarms_says_so_when_none_match(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr('tail_cw.cli.client_pool', _fake_client_pool)
    monkeypatch.setattr('tail_cw.cli.describe_alarms', _async_iter_factory([]))

    result = run_cli(['export', 'alarms', '--config', str(_write_config_file(tmp_path))], None, is_tty=False)

    assert result == 0
    assert 'No alarms matched' in capsys.readouterr().err


def test_run_cli_export_metrics_builds_a_dimensioned_query(tmp_path, capsys, monkeypatch):
    captured: dict[str, object] = {}

    async def fake_fetch(_client, queries, start_time, end_time):
        captured.update({'queries': queries, 'start': start_time, 'end': end_time})
        return [MetricSeries(id='m0', label='MemoryUtilization', timestamps=[NOW], values=[90.5])]

    monkeypatch.setattr('tail_cw.cli.client_pool', _fake_client_pool)
    monkeypatch.setattr('tail_cw.cli.fetch_metric_data', fake_fetch)
    argv = [
        'export',
        'metrics',
        '--namespace',
        'AWS/ECS',
        '--metric',
        'MemoryUtilization',
        '--dimension',
        'ClusterName=c1',
        '--dimension',
        'ServiceName=s1',
        '--stat',
        'Maximum',
        '--period',
        '300',
        '--config',
        str(_write_config_file(tmp_path)),
    ]

    result = run_cli(argv, None, is_tty=False)

    assert result == 0
    metric = captured['queries'][0]['MetricStat']  # type: ignore[index]
    assert metric['Metric']['Namespace'] == 'AWS/ECS'
    assert metric['Metric']['MetricName'] == 'MemoryUtilization'
    assert metric['Metric']['Dimensions'] == [
        {'Name': 'ClusterName', 'Value': 'c1'},
        {'Name': 'ServiceName', 'Value': 's1'},
    ]
    assert (metric['Stat'], metric['Period']) == ('Maximum', 300)
    record = json.loads(capsys.readouterr().out)
    assert record['datapoints'] == [{'timestamp': NOW.isoformat(), 'value': pytest.approx(90.5)}]


@pytest.mark.parametrize(
    ('metrics', 'expected_code', 'expected'),
    [
        (
            [
                {'MetricName': 'ApiRequestLatencyMs', 'Dimensions': [{'Name': 'Method', 'Value': 'POST'}]},
                {'MetricName': 'ApiRequestLatencyMs', 'Dimensions': [{'Name': 'StatusClass', 'Value': '5xx'}]},
            ],
            0,
            ['Method', 'StatusClass'],
        ),
        ([], 1, []),
    ],
)
def test_run_cli_export_dimensions_names_what_a_metric_publishes(
    tmp_path,
    capsys,
    monkeypatch,
    metrics,
    expected_code,
    expected,
):
    """Which dimensions a metric carries was only readable in the emitter's source."""
    monkeypatch.setattr('tail_cw.cli.client_pool', _fake_client_pool)
    monkeypatch.setattr(
        'tail_cw.cli.list_metric_definitions',
        _async_iter_factory(
            [
                MetricDefinition(
                    namespace='TailCwDemo',
                    name=metric['MetricName'],
                    dimensions=tuple(sorted((item['Name'], item['Value']) for item in metric['Dimensions'])),
                )
                for metric in metrics
            ],
        ),
    )
    argv = ['export', 'dimensions', '--namespace', 'TailCwDemo', '--config', str(_write_config_file(tmp_path))]

    result = run_cli(argv, None, is_tty=False)

    captured = capsys.readouterr()
    assert result == expected_code
    names = [name for line in captured.out.splitlines() for name in json.loads(line)['dimension_names']]
    assert names == expected
    if expected_code:
        assert 'No metrics published' in captured.err


def test_run_cli_export_metrics_rejects_a_dimension_without_a_value(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr('tail_cw.cli.client_pool', _fake_client_pool)
    argv = [
        'export',
        'metrics',
        '--namespace',
        'AWS/ECS',
        '--metric',
        'MemoryUtilization',
        '--dimension',
        'ClusterName',
        '--config',
        str(_write_config_file(tmp_path)),
    ]

    result = run_cli(argv, None, is_tty=False)

    assert result == 2
    assert 'expects NAME=VALUE' in capsys.readouterr().err
