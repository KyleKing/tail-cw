"""Tests for the CloudWatch Logs StartLiveTail wrapper."""

from collections.abc import Callable, Iterator
from datetime import UTC
from typing import Any
from unittest.mock import patch

import pytest
from botocore.exceptions import EventStreamError  # type: ignore[import-untyped]

from tail_cw.aws.live_tail import (
    LiveTailSessionError,
    _live_event_to_log_event,
    resolve_log_group_arns,
    stream_live_tail,
)

ACCOUNT_ARN_PREFIX = 'arn:aws:logs:us-east-1:123456789012:log-group:'


@pytest.fixture(autouse=True)
def _hermetic_aws_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('AWS_PROFILE', raising=False)
    monkeypatch.setenv('AWS_ACCESS_KEY_ID', 'testing')
    monkeypatch.setenv('AWS_SECRET_ACCESS_KEY', 'testing')
    monkeypatch.setenv('AWS_DEFAULT_REGION', 'us-east-1')


def _make_live_event(
    log_stream_name: str = 'stream-1',
    timestamp: int = 1700000000000,
    message: str = 'live message',
    log_group_identifier: str = f'{ACCOUNT_ARN_PREFIX}/test/group',
    *,
    include_ingestion_time: bool = True,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        'logStreamName': log_stream_name,
        'logGroupIdentifier': log_group_identifier,
        'message': message,
        'timestamp': timestamp,
    }
    if include_ingestion_time:
        event['ingestionTime'] = timestamp + 500
    return event


def _session_update(events: list[dict[str, Any]], *, sampled: bool = False) -> dict[str, Any]:
    return {'sessionUpdate': {'sessionMetadata': {'sampled': sampled}, 'sessionResults': events}}


def _timeout_error() -> EventStreamError:
    return EventStreamError(
        {'Error': {'Code': 'SessionTimeoutException', 'Message': 'session timed out'}},
        'StartLiveTail',
    )


class _FakePaginator:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self._pages = pages

    def paginate(self, **kwargs: Any) -> Iterator[dict[str, Any]]:
        prefix = kwargs['logGroupNamePrefix']
        for page in self._pages:
            groups = [group for group in page.get('logGroups', []) if group['logGroupName'].startswith(prefix)]
            yield {'logGroups': groups}


class _FakeLogsClient:
    def __init__(
        self,
        describe_pages: list[dict[str, Any]],
        streams: list[Callable[[], Iterator[dict[str, Any]]]],
    ) -> None:
        self._describe_pages = describe_pages
        self._streams = streams
        self.start_calls: list[dict[str, Any]] = []

    def get_paginator(self, name: str) -> _FakePaginator:
        assert name == 'describe_log_groups'
        return _FakePaginator(self._describe_pages)

    def start_live_tail(self, **kwargs: Any) -> dict[str, Any]:
        self.start_calls.append(kwargs)
        return {'responseStream': self._streams.pop(0)()}


def _describe_page(name: str, *, with_log_group_arn: bool = False) -> dict[str, Any]:
    group: dict[str, Any] = {'logGroupName': name, 'arn': f'{ACCOUNT_ARN_PREFIX}{name}:*'}
    if with_log_group_arn:
        group['logGroupArn'] = f'{ACCOUNT_ARN_PREFIX}{name}'
    return {'logGroups': [group]}


def test_resolve_log_group_arns_strips_trailing_wildcard():
    client = _FakeLogsClient([_describe_page('/test/group')], [])

    arns = resolve_log_group_arns(client, ['/test/group'])

    assert arns == [f'{ACCOUNT_ARN_PREFIX}/test/group']


def test_resolve_log_group_arns_prefers_log_group_arn_field():
    client = _FakeLogsClient([_describe_page('/test/group', with_log_group_arn=True)], [])

    arns = resolve_log_group_arns(client, ['/test/group'])

    assert arns == [f'{ACCOUNT_ARN_PREFIX}/test/group']


def test_resolve_log_group_arns_exact_match_only():
    pages = [
        {
            'logGroups': [
                {'logGroupName': '/test/group-extended', 'arn': f'{ACCOUNT_ARN_PREFIX}/test/group-extended:*'},
                {'logGroupName': '/test/group', 'arn': f'{ACCOUNT_ARN_PREFIX}/test/group:*'},
            ],
        },
    ]
    client = _FakeLogsClient(pages, [])

    arns = resolve_log_group_arns(client, ['/test/group'])

    assert arns == [f'{ACCOUNT_ARN_PREFIX}/test/group']


def test_resolve_log_group_arns_missing_group_raises():
    client = _FakeLogsClient([{'logGroups': []}], [])

    with pytest.raises(ValueError, match='Log group not found'):
        resolve_log_group_arns(client, ['/missing/group'])


def test_live_event_conversion_resolves_group_name_from_arn():
    raw = _make_live_event()

    event = _live_event_to_log_event(raw)

    assert event.log_group == '/test/group'
    assert event.log_stream == 'stream-1'
    assert event.message == 'live message'
    assert event.timestamp.tzinfo == UTC
    assert int(event.timestamp.timestamp() * 1000) == 1700000000000
    assert event.ingestion_time is not None
    assert event.event_id.startswith('live-1700000000000-')


def test_live_event_conversion_without_ingestion_time():
    raw = _make_live_event(include_ingestion_time=False)

    event = _live_event_to_log_event(raw)

    assert event.ingestion_time is None


def test_live_event_conversion_is_deterministic():
    first = _live_event_to_log_event(_make_live_event())
    second = _live_event_to_log_event(_make_live_event())
    other = _live_event_to_log_event(_make_live_event(message='different'))

    assert first.event_id == second.event_id
    assert first.event_id != other.event_id


def test_stream_live_tail_yields_events_and_passes_filter():
    def stream() -> Iterator[dict[str, Any]]:
        yield {'sessionStart': {'sessionId': 'abc'}}
        yield _session_update([_make_live_event(message='one'), _make_live_event(message='two')])
        yield _session_update([])

    client = _FakeLogsClient([_describe_page('/test/group')], [stream])

    with patch('tail_cw.aws.live_tail._create_logs_client', return_value=client):
        events = list(stream_live_tail(['/test/group'], filter_pattern='ERROR'))

    assert [event.message for event in events] == ['one', 'two']
    assert client.start_calls == [
        {
            'logGroupIdentifiers': [f'{ACCOUNT_ARN_PREFIX}/test/group'],
            'logEventFilterPattern': 'ERROR',
        },
    ]


def test_stream_live_tail_reconnects_after_stream_error():
    def failing_stream() -> Iterator[dict[str, Any]]:
        yield _session_update([_make_live_event(message='before-error')])
        raise _timeout_error()

    def healthy_stream() -> Iterator[dict[str, Any]]:
        yield _session_update([_make_live_event(message='after-reconnect')])

    client = _FakeLogsClient([_describe_page('/test/group')], [failing_stream, healthy_stream])

    with patch('tail_cw.aws.live_tail._create_logs_client', return_value=client):
        events = list(stream_live_tail(['/test/group']))

    assert [event.message for event in events] == ['before-error', 'after-reconnect']
    assert len(client.start_calls) == 2


def test_stream_live_tail_raises_after_exhausted_retries():
    def failing_stream() -> Iterator[dict[str, Any]]:
        yield _session_update([])
        raise _timeout_error()

    client = _FakeLogsClient(
        [_describe_page('/test/group')],
        [failing_stream, failing_stream, failing_stream],
    )

    with (
        patch('tail_cw.aws.live_tail._create_logs_client', return_value=client),
        pytest.raises(LiveTailSessionError, match='after 2 reconnect attempts'),
    ):
        list(stream_live_tail(['/test/group'], max_reconnect_attempts=2))

    assert len(client.start_calls) == 3


def test_stream_live_tail_resets_failure_count_on_progress():
    def failing_stream() -> Iterator[dict[str, Any]]:
        yield _session_update([_make_live_event(message='progress')])
        raise _timeout_error()

    def final_stream() -> Iterator[dict[str, Any]]:
        yield _session_update([_make_live_event(message='done')])

    client = _FakeLogsClient(
        [_describe_page('/test/group')],
        [failing_stream, failing_stream, failing_stream, final_stream],
    )

    with patch('tail_cw.aws.live_tail._create_logs_client', return_value=client):
        events = list(stream_live_tail(['/test/group'], max_reconnect_attempts=1))

    assert [event.message for event in events] == ['progress', 'progress', 'progress', 'done']


def test_stream_live_tail_reports_sampling():
    def stream() -> Iterator[dict[str, Any]]:
        yield _session_update([_make_live_event()], sampled=True)

    client = _FakeLogsClient([_describe_page('/test/group')], [stream])
    sampled_flags: list[bool] = []

    with patch('tail_cw.aws.live_tail._create_logs_client', return_value=client):
        list(stream_live_tail(['/test/group'], on_sampled=sampled_flags.append))

    assert sampled_flags == [True]


@pytest.mark.parametrize('group_count', [0, 11])
def test_stream_live_tail_rejects_invalid_group_count(group_count):
    log_groups = [f'/test/group-{index}' for index in range(group_count)]

    with pytest.raises(ValueError, match='1 to 10 log groups'):
        list(stream_live_tail(log_groups))
