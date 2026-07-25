"""Tests for the recently opened log group history."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from tail_cw.aws.log_groups import LogGroupInfo
from tail_cw.recents import (
    DEFAULT_PROFILE_KEY,
    RECENTS_FILENAME,
    Recents,
    load_recents,
    profile_recents,
    recents_path,
    record_selection,
    save_recents,
    sort_by_recency,
)

_NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)


def _group(name: str) -> LogGroupInfo:
    return LogGroupInfo(name=name, arn=f'arn:{name}', stored_bytes=None, retention_days=None, created=_NOW)


def test_recents_path_names_the_data_file():
    path = recents_path()

    assert path.name == RECENTS_FILENAME
    assert path.parent.name == 'tail-cw'


def test_record_selection_puts_the_newest_first():
    recents = record_selection(Recents(), ['/a'], profile=None)
    updated = record_selection(recents, ['/b'], profile=None)

    assert updated.by_profile[DEFAULT_PROFILE_KEY] == ('/b', '/a')


def test_record_selection_keeps_the_order_within_one_call():
    recents = record_selection(Recents(), ['/b', '/a', '/c'], profile=None)

    assert recents.by_profile[DEFAULT_PROFILE_KEY] == ('/b', '/a', '/c')


def test_record_selection_deduplicates_within_and_across_calls():
    recents = record_selection(Recents(), ['/a', '/b', '/a'], profile=None)
    updated = record_selection(recents, ['/b'], profile=None)

    assert recents.by_profile[DEFAULT_PROFILE_KEY] == ('/a', '/b')
    assert updated.by_profile[DEFAULT_PROFILE_KEY] == ('/b', '/a')


def test_record_selection_truncates_to_the_limit():
    recents = record_selection(Recents(), [f'/g{index}' for index in range(5)], profile=None, limit=3)

    assert recents.by_profile[DEFAULT_PROFILE_KEY] == ('/g0', '/g1', '/g2')


def test_record_selection_of_nothing_changes_nothing():
    recents = record_selection(Recents(), ['/a'], profile=None)

    assert record_selection(recents, [], profile=None) == recents


def test_record_selection_leaves_the_input_untouched():
    recents = Recents(by_profile={DEFAULT_PROFILE_KEY: ('/a',)})
    record_selection(recents, ['/b'], profile=None)

    assert recents.by_profile[DEFAULT_PROFILE_KEY] == ('/a',)


def test_profiles_keep_separate_histories():
    recents = record_selection(Recents(), ['/prod'], profile='prod')
    recents = record_selection(recents, ['/dev'], profile='dev')

    assert profile_recents(recents, 'prod') == ('/prod',)
    assert profile_recents(recents, 'dev') == ('/dev',)
    assert profile_recents(recents, None) == ()


def test_an_empty_profile_name_shares_the_default_key():
    recents = record_selection(Recents(), ['/a'], profile='')

    assert profile_recents(recents, None) == ('/a',)


def test_sort_by_recency_leads_with_recents_in_order():
    groups = [_group('/a'), _group('/b'), _group('/c')]

    sorted_groups = sort_by_recency(groups, ['/c', '/a'])

    assert [group.name for group in sorted_groups] == ['/c', '/a', '/b']


def test_sort_by_recency_keeps_the_rest_in_original_order():
    groups = [_group('/a'), _group('/b'), _group('/c'), _group('/d')]

    sorted_groups = sort_by_recency(groups, ['/c'])

    assert [group.name for group in sorted_groups] == ['/c', '/a', '/b', '/d']


def test_sort_by_recency_ignores_groups_that_no_longer_exist():
    groups = [_group('/a')]

    sorted_groups = sort_by_recency(groups, ['/gone', '/a'])

    assert [group.name for group in sorted_groups] == ['/a']


def test_sort_by_recency_without_history_is_the_identity():
    groups = [_group('/a'), _group('/b')]

    assert sort_by_recency(groups, []) == groups


def test_save_then_load_round_trips(tmp_path: Path):
    path = tmp_path / 'nested' / 'recents.json'
    recents = record_selection(Recents(), ['/a', '/b'], profile='prod')

    save_recents(recents, path)

    assert load_recents(path) == recents


def test_saving_twice_replaces_the_file(tmp_path: Path):
    path = tmp_path / 'recents.json'
    save_recents(record_selection(Recents(), ['/a'], profile=None), path)
    save_recents(record_selection(Recents(), ['/b'], profile=None), path)

    assert profile_recents(load_recents(path), None) == ('/b',)


def test_a_missing_file_loads_as_empty(tmp_path: Path):
    assert load_recents(tmp_path / 'absent.json') == Recents()


def test_corrupt_json_loads_as_empty(tmp_path: Path):
    path = tmp_path / 'recents.json'
    path.write_text('{not json', encoding='utf-8')

    assert load_recents(path) == Recents()


def test_a_non_object_payload_loads_as_empty(tmp_path: Path):
    path = tmp_path / 'recents.json'
    path.write_text('["/a"]', encoding='utf-8')

    assert load_recents(path) == Recents()


def test_a_malformed_entry_is_dropped_without_losing_the_rest(tmp_path: Path):
    path = tmp_path / 'recents.json'
    path.write_text(json.dumps({'prod': ['/a', 7], 'dev': 'nope'}), encoding='utf-8')

    recents = load_recents(path)

    assert recents.by_profile == {'prod': ('/a',)}


def test_load_uses_the_default_path_when_none_is_given(recents_file: Path):
    recents_file.write_text(json.dumps({'': ['/a']}), encoding='utf-8')

    assert profile_recents(load_recents(), None) == ('/a',)


def test_save_uses_the_default_path_when_none_is_given(recents_file: Path):
    save_recents(record_selection(Recents(), ['/a'], profile=None))

    assert json.loads(recents_file.read_text(encoding='utf-8')) == {'': ['/a']}
