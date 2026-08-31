"""Tests for counting cached events by payload field."""

from pathlib import Path

import pytest

from tail_cw.cache.storage import write_log_events_to_parquet
from tail_cw.query.expression import parse_query
from tail_cw.query.facets import (
    NULL_LABEL,
    count_by_field,
    discover_field_paths,
    normalize_field_path,
)
from tests.factories import make_events


def _written(tmp_path: Path, messages: list[str], *, name: str = 'events.parquet') -> Path:
    path = tmp_path / name
    write_log_events_to_parquet(make_events(messages), path)
    return path


@pytest.mark.parametrize(
    ('field', 'expected'),
    [
        ('level', ('level',)),
        ('parsed.level', ('level',)),
        ('parsed.http.status', ('http', 'status')),
        ('', ()),
    ],
)
def test_normalize_field_path_tolerates_the_parsed_prefix(field, expected):
    assert normalize_field_path(field) == expected


def test_count_by_field_ranks_values_by_count(tmp_path: Path):
    path = _written(tmp_path, ['{"level":"info"}'] * 3 + ['{"level":"error"}'])

    facet = count_by_field([path], 'level', top=5)

    assert facet.path == 'level'
    assert [(value.value, value.count) for value in facet.values] == [('info', 3), ('error', 1)]
    assert facet.present == 4
    assert facet.truncated is False


def test_count_by_field_reaches_a_nested_key(tmp_path: Path):
    path = _written(tmp_path, ['{"http":{"status":200}}', '{"http":{"status":500}}'])

    facet = count_by_field([path], 'http.status', top=5)

    assert {value.value for value in facet.values} == {'200', '500'}


def test_count_by_field_counts_the_records_missing_the_field(tmp_path: Path):
    """A field half the records carry is a different fact from one they all carry."""
    path = _written(tmp_path, ['{"level":"info"}', '{"other":1}'])

    facet = count_by_field([path], 'level', top=5)

    assert {value.value: value.count for value in facet.values} == {'info': 1, NULL_LABEL: 1}
    assert facet.present == 1


def test_count_by_field_says_when_it_named_only_the_top_values(tmp_path: Path):
    path = _written(tmp_path, [f'{{"id":"{index}"}}' for index in range(5)])

    facet = count_by_field([path], 'id', top=2)

    assert len(facet.values) == 2
    assert facet.distinct == 5
    assert facet.truncated is True


def test_count_by_field_merges_several_files(tmp_path: Path):
    first = _written(tmp_path, ['{"level":"info"}'], name='one.parquet')
    second = _written(tmp_path, ['{"level":"info"}', '{"level":"warn"}'], name='two.parquet')

    facet = count_by_field([first, second], 'level', top=5)

    assert [(value.value, value.count) for value in facet.values] == [('info', 2), ('warn', 1)]


def test_count_by_field_skips_a_file_whose_payload_lacks_the_field(tmp_path: Path):
    """Both engines raise on an absent struct field, which once failed the whole read."""
    present = _written(tmp_path, ['{"level":"info"}'], name='present.parquet')
    absent = _written(tmp_path, ['plain text'], name='absent.parquet')

    facet = count_by_field([absent, present], 'level', top=5)

    assert [(value.value, value.count) for value in facet.values] == [('info', 1)]


def test_count_by_field_applies_a_filter(tmp_path: Path):
    path = _written(tmp_path, ['{"level":"info","svc":"api"}', '{"level":"error","svc":"api"}'])

    facet = count_by_field([path], 'svc', filter_node=parse_query('level:error'), top=5)

    assert [(value.value, value.count) for value in facet.values] == [('api', 1)]


def test_count_by_field_applies_a_text_filter(tmp_path: Path):
    """A text search reads a column the file does not have, so the source has to derive it."""
    path = _written(tmp_path, ['{"level":"info","msg":"upstream timeout"}', '{"level":"info","msg":"ok"}'])

    facet = count_by_field([path], 'level', filter_node=parse_query('timeout'), top=5)

    assert [(value.value, value.count) for value in facet.values] == [('info', 1)]


def test_discover_field_paths_ranks_the_fields_the_files_share(tmp_path: Path):
    shared = _written(tmp_path, ['{"level":"info","svc":"api"}'], name='shared.parquet')
    only = _written(tmp_path, ['{"level":"info"}'], name='only.parquet')

    assert discover_field_paths([shared, only], limit=10) == ['level', 'svc']


def test_discover_field_paths_flattens_nested_objects(tmp_path: Path):
    path = _written(tmp_path, ['{"http":{"status":200,"method":"GET"}}'])

    assert discover_field_paths([path], limit=10) == ['http.status', 'http.method']


def test_discover_field_paths_finds_nothing_in_unstructured_logs(tmp_path: Path):
    path = _written(tmp_path, ['plain text', 'more text'])

    assert discover_field_paths([path], limit=10) == []
