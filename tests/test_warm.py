"""Tests for the scheduled cache-warming command."""

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tail_cw import cli
from tail_cw.aws.events import LogEvent
from tail_cw.cli import run_cli
from tail_cw.warm import command_lock, load_marks, save_marks
from tests.factories import make_event


def _write_config(tmp_path: Path, groups: list[str] | None = None) -> Path:
    config = tmp_path / 'config.toml'
    warm = f'[warm]\ngroups = {json.dumps(groups if groups is not None else ["/g"])}\nwindow = "2h"\n'
    config.write_text(f'[cache]\ncache_dir = "{tmp_path}/cache"\n{warm}', encoding='utf-8')
    return config


class _FakeFetcher:
    def __init__(self, events) -> None:
        self._events = events
        self.calls: list[tuple[str, datetime, datetime]] = []

    async def __call__(self, _client, log_group, start_time, end_time, **_kwargs) -> AsyncIterator[LogEvent]:
        self.calls.append((log_group, start_time, end_time))
        for event in self._events:
            yield event


def test_marks_round_trip_and_a_corrupt_file_starts_cold(tmp_path: Path):
    marks = {'/g': datetime(2026, 7, 25, 12, 0, tzinfo=UTC)}
    save_marks(tmp_path, marks)
    assert load_marks(tmp_path) == marks

    (tmp_path / 'warm-marks.json').write_text('{not json', encoding='utf-8')
    assert load_marks(tmp_path) == {}


def test_a_second_command_cannot_take_the_lock(tmp_path: Path):
    with command_lock(tmp_path) as held:
        assert held is True
        with command_lock(tmp_path) as second:
            assert second is False


def test_warm_fetches_since_the_last_mark(tmp_path, capsys, monkeypatch):
    config = _write_config(tmp_path)
    monkeypatch.setattr(cli, 'on_battery', lambda: False)
    now = datetime.now(UTC)
    events = [make_event('{"level":"INFO"}', timestamp=now - timedelta(minutes=5))]

    fetcher = _FakeFetcher(events)
    result = run_cli(['warm', '--config', str(config)], None, fetch_events=fetcher, is_tty=False)

    assert result == 0
    assert fetcher.calls
    first_start = fetcher.calls[0][1]
    assert first_start <= now - timedelta(hours=1, minutes=59)
    assert load_marks(tmp_path / 'cache')['/g'] is not None

    # The next run starts where this one ended, not at the cold window.
    fetcher.calls.clear()
    result = run_cli(['warm', '--config', str(config)], None, fetch_events=fetcher, is_tty=False)

    assert result == 0
    assert fetcher.calls[0][1] > first_start


def test_warm_skips_on_battery(tmp_path, capsys, monkeypatch):
    config = _write_config(tmp_path)
    monkeypatch.setattr(cli, 'on_battery', lambda: True)
    fetcher = _FakeFetcher([make_event()])

    result = run_cli(['warm', '--config', str(config)], None, fetch_events=fetcher, is_tty=False)

    assert result == 0
    assert fetcher.calls == []
    assert 'on battery' in capsys.readouterr().err


def test_warm_skips_while_a_command_runs(tmp_path, capsys, monkeypatch):
    config = _write_config(tmp_path)
    monkeypatch.setattr(cli, 'on_battery', lambda: False)
    fetcher = _FakeFetcher([make_event()])

    with command_lock(tmp_path / 'cache') as held:
        assert held is True
        result = run_cli(['warm', '--config', str(config)], None, fetch_events=fetcher, is_tty=False)

    assert result == 0
    assert fetcher.calls == []
    assert 'command is running' in capsys.readouterr().err


def test_warm_with_no_groups_is_a_quiet_noop(tmp_path, capsys):
    config = tmp_path / 'config.toml'
    config.write_text(f'[cache]\ncache_dir = "{tmp_path}/cache"\n', encoding='utf-8')

    result = run_cli(['warm', '--config', str(config)], None, is_tty=False)

    assert result == 0
    assert 'nothing to do' in capsys.readouterr().err
