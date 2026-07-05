"""Unit tests for the CLI module (time parsing, arg parsing, fetch pipeline)."""

import io
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tail_cw.aws.client import LogEvent
from tail_cw.cli import (
    FetchRequest,
    build_parser,
    parse_time,
    resolve_parquet_path,
    run_cli,
    write_ndjson,
)
from tail_cw.config import CacheConfig, TailCWConfig

NOW = datetime(2026, 7, 5, 12, 0, 0, tzinfo=UTC)


def _make_events(count: int = 3) -> list[LogEvent]:
    return [
        LogEvent(
            log_group='/aws/test/group',
            log_stream='stream-1',
            timestamp=NOW - timedelta(minutes=count - index),
            message=f'{{"level":"INFO","index":{index}}}',
            event_id=f'event-{index:04d}',
            ingestion_time=None,
        )
        for index in range(count)
    ]


class _FakeFetcher:
    def __init__(self, events: list[LogEvent]) -> None:
        self.events = events
        self.calls: list[dict[str, object]] = []

    def __call__(self, log_group, start_time, end_time, **kwargs) -> Iterator[LogEvent]:
        self.calls.append({'log_group': log_group, 'start_time': start_time, 'end_time': end_time, **kwargs})
        return iter(self.events)


class _RecordingTui:
    def __init__(self) -> None:
        self.calls: list[tuple[TailCWConfig, Path, FetchRequest]] = []

    def __call__(self, config, parquet_path, request) -> None:
        self.calls.append((config, parquet_path, request))


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


def test_build_parser_fetch_defaults():
    args = build_parser().parse_args(['fetch', '/aws/lambda/fn'])

    assert args.command == 'fetch'
    assert args.log_group == '/aws/lambda/fn'
    assert args.start == '1h'
    assert args.end is None
    assert args.filter_pattern is None
    assert args.profile is None
    assert args.region is None
    assert args.config_path is None
    assert args.json_output is False
    assert args.no_cache is False


def test_build_parser_fetch_all_flags(tmp_path):
    args = build_parser().parse_args(
        [
            'fetch',
            '/aws/lambda/fn',
            '--start',
            '30m',
            '--end',
            '2026-07-05T12:00:00Z',
            '--filter',
            'ERROR',
            '--profile',
            'dev',
            '--region',
            'us-west-2',
            '--config',
            str(tmp_path / 'config.toml'),
            '--json',
            '--no-cache',
        ]
    )

    assert args.start == '30m'
    assert args.end == '2026-07-05T12:00:00Z'
    assert args.filter_pattern == 'ERROR'
    assert args.profile == 'dev'
    assert args.region == 'us-west-2'
    assert args.config_path == tmp_path / 'config.toml'
    assert args.json_output is True
    assert args.no_cache is True


def test_build_parser_no_subcommand():
    args = build_parser().parse_args([])

    assert args.command is None


def test_resolve_parquet_path_fetches_on_miss(tmp_path):
    fetcher = _FakeFetcher(_make_events())

    result = resolve_parquet_path(_make_request(), _make_config(tmp_path), fetch_events=fetcher)

    assert result is not None
    assert result.exists()
    assert len(fetcher.calls) == 1


def test_resolve_parquet_path_uses_cache_on_hit(tmp_path):
    config = _make_config(tmp_path)
    request = _make_request()
    first_path = resolve_parquet_path(request, config, fetch_events=_FakeFetcher(_make_events()))

    second_fetcher = _FakeFetcher(_make_events())
    second_path = resolve_parquet_path(request, config, fetch_events=second_fetcher)

    assert second_path == first_path
    assert second_fetcher.calls == []


def test_resolve_parquet_path_no_cache_refetches(tmp_path):
    config = _make_config(tmp_path)
    request = _make_request()
    resolve_parquet_path(request, config, fetch_events=_FakeFetcher(_make_events()))

    refetcher = _FakeFetcher(_make_events())
    result = resolve_parquet_path(request, config, use_cache=False, fetch_events=refetcher)

    assert result is not None
    assert len(refetcher.calls) == 1


def test_resolve_parquet_path_empty_fetch_returns_none(tmp_path):
    fetcher = _FakeFetcher([])

    result = resolve_parquet_path(_make_request(), _make_config(tmp_path), fetch_events=fetcher)

    assert result is None


def test_resolve_parquet_path_threads_fetch_parameters(tmp_path):
    fetcher = _FakeFetcher(_make_events())
    request = _make_request(filter_pattern='ERROR', profile='dev', region='us-west-2')

    resolve_parquet_path(request, _make_config(tmp_path), fetch_events=fetcher)

    call = fetcher.calls[0]
    assert call['log_group'] == '/aws/test/group'
    assert call['filter_pattern'] == 'ERROR'
    assert call['profile_name'] == 'dev'
    assert call['region_name'] == 'us-west-2'


def test_resolve_parquet_path_profile_changes_cache_entry(tmp_path):
    config = _make_config(tmp_path)
    request = _make_request()
    resolve_parquet_path(request, config, fetch_events=_FakeFetcher(_make_events()))

    profiled_fetcher = _FakeFetcher(_make_events())
    resolve_parquet_path(_make_request(profile='dev'), config, fetch_events=profiled_fetcher)

    assert len(profiled_fetcher.calls) == 1


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


def _write_config_file(tmp_path: Path) -> Path:
    config_path = tmp_path / 'config.toml'
    cache_dir = tmp_path / 'cache'
    config_path.write_text(f'[cache]\ncache_dir = "{cache_dir}"\n', encoding='utf-8')
    return config_path


def test_run_cli_no_subcommand_prints_help(capsys):
    tui = _RecordingTui()

    result = run_cli([], tui)

    assert result == 2
    assert 'usage: tail-cw' in capsys.readouterr().err
    assert tui.calls == []


def test_run_cli_invalid_start(tmp_path, capsys):
    result = run_cli(['fetch', '/g', '--start', 'bogus'], _RecordingTui())

    assert result == 2
    assert 'Invalid time' in capsys.readouterr().err


def test_run_cli_start_after_end(capsys):
    result = run_cli(['fetch', '/g', '--start', '1h', '--end', '2h'], _RecordingTui())

    assert result == 2
    assert 'must be before' in capsys.readouterr().err


def test_run_cli_bad_config(tmp_path, capsys):
    config_path = tmp_path / 'config.toml'
    config_path.write_text('not valid toml [', encoding='utf-8')

    result = run_cli(['fetch', '/g', '--config', str(config_path)], _RecordingTui())

    assert result == 1
    assert 'Configuration error' in capsys.readouterr().err


def test_run_cli_json_mode(tmp_path, capsys):
    config_path = _write_config_file(tmp_path)
    tui = _RecordingTui()
    fetcher = _FakeFetcher(_make_events(3))

    result = run_cli(
        ['fetch', '/aws/test/group', '--json', '--config', str(config_path)],
        tui,
        fetch_events=fetcher,
    )

    assert result == 0
    assert tui.calls == []
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 3
    records = [json.loads(line) for line in lines]
    assert {record['event_id'] for record in records} == {'event-0000', 'event-0001', 'event-0002'}


def test_run_cli_json_mode_no_events(tmp_path, capsys):
    config_path = _write_config_file(tmp_path)

    result = run_cli(
        ['fetch', '/aws/test/group', '--json', '--config', str(config_path)],
        _RecordingTui(),
        fetch_events=_FakeFetcher([]),
    )

    assert result == 0
    captured = capsys.readouterr()
    assert not captured.out
    assert 'No events found' in captured.err


def test_run_cli_tui_mode(tmp_path):
    config_path = _write_config_file(tmp_path)
    tui = _RecordingTui()

    result = run_cli(
        ['fetch', '/aws/test/group', '--profile', 'dev', '--config', str(config_path)],
        tui,
        fetch_events=_FakeFetcher(_make_events()),
    )

    assert result == 0
    assert len(tui.calls) == 1
    _config, parquet_path, request = tui.calls[0]
    assert parquet_path.exists()
    assert request.log_group == '/aws/test/group'
    assert request.profile == 'dev'
    assert request.end_time - request.start_time == timedelta(hours=1)
