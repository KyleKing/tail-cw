"""Tests for message pattern clustering."""

import pytest

from tail_cw.query.patterns import MessagePattern, cluster_messages, normalize_message

_UUID = '550e8400-e29b-41d4-a716-446655440000'

_LAMBDA_SAMPLE = [
    f'START RequestId: {_UUID} Version: $LATEST',
    '2025-01-01T12:00:00.123Z INFO request completed status=200 duration=12.5ms',
    '2025-01-01T12:00:00.456Z INFO request completed status=200 duration=8.1ms',
    '2025-01-01T12:00:00.789Z INFO request completed status=404 duration=3.25ms',
    '2025-01-01T12:00:01.001Z ERROR Timeout connecting to redis.internal:6379',
    f'END RequestId: {_UUID}',
    f'REPORT RequestId: {_UUID} Duration: 12.51 ms Billed Duration: 13 ms Memory Size: 128 MB',
    'START RequestId: 6ba7b810-9dad-11d1-80b4-00c04fd430c8 Version: $LATEST',
    'END RequestId: 6ba7b810-9dad-11d1-80b4-00c04fd430c8',
]


@pytest.mark.parametrize(
    ('message', 'expected'),
    [
        ('at 2025-01-01T12:00:00Z done', 'at <ts> done'),
        ('at 2025-01-01 12:00:00.123456+00:00 done', 'at <ts> done'),
        ('epoch 1700000000000 done', 'epoch <ts> done'),
        ('epoch 1700000000 done', 'epoch <ts> done'),
        (f'id {_UUID} done', 'id <uuid> done'),
        (f'id {_UUID.upper()} done', 'id <uuid> done'),
        ('sha deadbeefcafe1234 done', 'sha <hex> done'),
        ('cache "hit" done', 'cache <str> done'),
        ("cache 'hit' done", 'cache <str> done'),
        ('took 12.5 ms', 'took <n> ms'),
        ('took 1.5e3 ms', 'took <n> ms'),
        ('status 200 ms', 'status <n> ms'),
        ('offset -42 ms', 'offset -<n> ms'),
    ],
)
def test_normalize_message_placeholder_classes(message, expected):
    assert normalize_message(message) == expected


@pytest.mark.parametrize(
    ('message', 'expected'),
    [
        (f'req={_UUID}', 'req=<uuid>'),
        ('req=deadbeefcafe1234', 'req=<hex>'),
        (f'{_UUID} deadbeefcafe1234 7', '<uuid> <hex> <n>'),
    ],
)
def test_normalize_message_ordering_hazard(message, expected):
    shape = normalize_message(message)

    assert shape == expected
    assert shape.count('<uuid>') == expected.count('<uuid>')
    assert shape.count('<hex>') == expected.count('<hex>')
    assert shape.count('<n>') == expected.count('<n>')


def test_jsonl_pair_shares_shape():
    first = normalize_message('{"level":"ERROR","ms":12}')
    second = normalize_message('{"level":"ERROR","ms":900}')

    assert first == second
    assert first == '{"level":"ERROR","ms":<n>}'


def test_jsonl_reordered_keys_share_shape():
    assert normalize_message('{"level":"ERROR","ms":12}') == normalize_message('{"ms":900,"level":"ERROR"}')


def test_jsonl_nested_object_values_normalize():
    shape = normalize_message(f'{{"level":"INFO","req":{{"id":"{_UUID}","attempt":3,"ok":true}}}}')

    assert shape == '{"level":"INFO","req":{"attempt":<n>,"id":<uuid>,"ok":true}}'


def test_jsonl_with_timestamp_prefix_keeps_placeholder():
    assert normalize_message('2025-01-01T12:00:00Z {"count":5}') == '<ts> {"count":<n>}'


def test_non_json_text_with_json_fragment():
    assert normalize_message('Received payload {"id": 5} from client') == 'Received payload {<str>: <n>} from client'


def test_malformed_json_falls_back_to_text():
    assert normalize_message('{"level":"ERROR", oops 12') == '{<str>:<str>, oops <n>'


def test_whitespace_collapses():
    assert normalize_message('  start \t  of   line\n') == 'start of line'


def test_cluster_messages_empty_input():
    assert cluster_messages([]) == []


def test_cluster_messages_counts_and_example():
    patterns = cluster_messages(['status 200', 'status 404', 'other'])

    assert patterns == [
        MessagePattern(key='status <n>', count=2, example='status 200'),
        MessagePattern(key='other', count=1, example='other'),
    ]


def test_cluster_messages_limit_truncates():
    patterns = cluster_messages(['a 1', 'b 2', 'c 3', 'd 4'], limit=2)

    assert [pattern.key for pattern in patterns] == ['a <n>', 'b <n>']


def test_cluster_messages_ties_break_by_first_appearance():
    messages = ['zebra 1', 'apple 2', 'mango 3', 'apple 4', 'zebra 5']
    patterns = cluster_messages(messages)

    assert [pattern.key for pattern in patterns] == ['zebra <n>', 'apple <n>', 'mango <n>']
    assert [pattern.count for pattern in patterns] == [2, 2, 1]


def test_cluster_messages_lambda_sample():
    patterns = cluster_messages(_LAMBDA_SAMPLE)

    assert len(patterns) == 5
    assert patterns[0] == MessagePattern(
        key='<ts> INFO request completed status=<n> duration=<n>ms',
        count=3,
        example='2025-01-01T12:00:00.123Z INFO request completed status=200 duration=12.5ms',
    )
    assert [pattern.count for pattern in patterns] == [3, 2, 2, 1, 1]
    assert 'START RequestId: <uuid> Version: $LATEST' in {pattern.key for pattern in patterns}
