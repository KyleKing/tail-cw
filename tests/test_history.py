"""Tests for the shared rollup, alarm, and Insights history."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from tail_cw.history import (
    DEFAULT_HISTORY_LIMIT,
    MAX_DETAIL_CHARS,
    HistoryKind,
    append,
    load_history,
    make_entry,
    record,
    save_history,
)

_RECORDED = datetime(2026, 8, 21, 18, 15, tzinfo=UTC)


def _entry(title: str = 'filter @message like /boom/', kind: HistoryKind = HistoryKind.INSIGHTS):
    return make_entry(kind, recorded=_RECORDED, title=title, window='1h', detail='| a |\n', profile='read-prod')


def test_an_entry_round_trips(tmp_path: Path):
    path = tmp_path / 'history.json'
    save_history([_entry()], path)

    assert load_history(path) == (_entry(),)


def test_a_long_result_is_truncated():
    entry = make_entry(HistoryKind.SUMMARY, recorded=_RECORDED, title='t', window='1h', detail='x' * 99_999)

    assert len(entry.detail) < MAX_DETAIL_CHARS + 100
    assert entry.detail.endswith('truncated')


def test_the_newest_entry_leads_and_the_oldest_falls_off():
    entries = tuple(_entry(f'query {index}') for index in range(DEFAULT_HISTORY_LIMIT))

    updated = record(entries, _entry('newest'))

    assert updated[0].title == 'newest'
    assert len(updated) == DEFAULT_HISTORY_LIMIT
    assert 'query 49' not in [entry.title for entry in updated]


@pytest.mark.parametrize('payload', ['not json at all', '{"not": "a list"}', '[{"kind": "unknown"}]', '[7]'])
def test_unreadable_history_degrades_to_empty(tmp_path: Path, payload):
    path = tmp_path / 'history.json'
    path.write_text(payload, encoding='utf-8')

    assert load_history(path) == ()


def test_a_missing_file_reads_as_empty(tmp_path: Path):
    assert load_history(tmp_path / 'absent.json') == ()


def test_append_never_fails_the_command_that_produced_it(tmp_path: Path):
    """History is a convenience, so an unwritable data directory must not raise."""
    blocked = tmp_path / 'file-not-a-dir'
    blocked.write_text('', encoding='utf-8')

    append(_entry(), blocked / 'history.json')
