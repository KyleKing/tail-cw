# ruff: file-ignore[unused-async] - the AWS fakes are async generators or awaitable stand-ins
"""Unit tests for the CLI module (time parsing, arg parsing, export pipelines)."""

import contextlib
import io
import json
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tail_cw.aws.client import LogEvent
from tail_cw.aws.dashboards import Dashboard, DashboardSummary, TextWidget, WidgetLayout
from tail_cw.aws.log_groups import LogGroupInfo
from tail_cw.cache.storage import read_parquet_to_log_events
from tail_cw.cli import (
    FetchRequest,
    Session,
    ShellSeed,
    TailRequest,
    build_parser,
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

NOW = datetime(2026, 7, 5, 12, 0, 0, tzinfo=UTC)


def _make_events(count: int = 3, *, log_group: str = '/aws/test/group') -> list[LogEvent]:
    return [
        LogEvent(
            log_group=log_group,
            log_stream='stream-1',
            timestamp=NOW - timedelta(minutes=count - index),
            message=f'{{"level":"INFO","index":{index}}}',
            event_id=f'event-{index:04d}',
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
    filter_pattern: str | None = None,
    profile: str | None = None,
    region: str | None = None,
) -> FetchRequest:
    return FetchRequest(
        log_group=log_group,
        start_time=NOW - timedelta(hours=1),
        end_time=NOW,
        filter_pattern=filter_pattern,
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

    result = await resolve_parquet_path(_CLIENT, _make_request(), _make_config(tmp_path), fetch_events=fetcher)

    assert result is not None
    assert result.exists()
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
    result = await resolve_parquet_path(_CLIENT, request, config, use_cache=False, fetch_events=refetcher)

    assert result is not None
    assert len(refetcher.calls) == 1


async def test_resolve_parquet_path_empty_fetch_returns_none(tmp_path):
    fetcher = _FakeFetcher([])

    result = await resolve_parquet_path(_CLIENT, _make_request(), _make_config(tmp_path), fetch_events=fetcher)

    assert result is None


async def test_resolve_parquet_path_threads_fetch_parameters(tmp_path):
    fetcher = _FakeFetcher(_make_events())
    request = _make_request(filter_pattern='ERROR', profile='dev', region='us-west-2')

    await resolve_parquet_path(_CLIENT, request, _make_config(tmp_path), fetch_events=fetcher)

    call = fetcher.calls[0]
    assert call['log_group'] == '/aws/test/group'
    assert call['filter_pattern'] == 'ERROR'
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

    assert len(paths) == 1
    assert paths[0].exists()
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
    assert record['event_id'] == 'event-0000'
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
        ['export', 'logs', '/aws/test/group', '--config', str(config_path)],
        _RecordingShell(),
        fetch_events=_FakeFetcher(_make_events(3)),
        is_tty=False,
    )

    assert result == 0
    lines = capsys.readouterr().out.strip().splitlines()
    records = [json.loads(line) for line in lines]
    assert {record['event_id'] for record in records} == {'event-0000', 'event-0001', 'event-0002'}


def test_run_cli_export_logs_no_cache_refetches(tmp_path, capsys):
    config_path = _write_config_file(tmp_path)
    argv = ['export', 'logs', '/aws/test/group', '--config', str(config_path)]
    run_cli(argv, None, fetch_events=_FakeFetcher(_make_events(2)), is_tty=False)
    capsys.readouterr()

    refetcher = _FakeFetcher(_make_events(2))
    result = run_cli([*argv, '--no-cache'], None, fetch_events=refetcher, is_tty=False)

    assert result == 0
    assert len(refetcher.calls) == 1


def test_run_cli_export_logs_no_events(tmp_path, capsys):
    config_path = _write_config_file(tmp_path)

    result = run_cli(
        ['export', 'logs', '/aws/test/group', '--config', str(config_path)],
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
            event_id='live-0001',
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
    assert [record['event_id'] for record in records] == ['event-0000', 'event-0001', 'live-0001']
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
