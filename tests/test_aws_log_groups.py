"""Tests for log group discovery and pattern resolution."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

from tail_cw.aws.log_groups import LogGroupInfo, describe_log_groups, resolve_group_pattern

ACCOUNT_ARN_PREFIX = 'arn:aws:logs:us-east-1:123456789012:log-group:'
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
CREATED = EPOCH + timedelta(days=20000, hours=5, minutes=30)


def _group(name: str) -> LogGroupInfo:
    return LogGroupInfo(
        name=name,
        arn=f'{ACCOUNT_ARN_PREFIX}{name}',
        stored_bytes=1024,
        retention_days=30,
        created=CREATED,
    )


def _names(groups: list[LogGroupInfo]) -> list[str]:
    return [group.name for group in groups]


GROUPS = [
    _group('/aws/lambda/api'),
    _group('/aws/lambda/api-handler'),
    _group('/aws/lambda/worker'),
    _group('/ecs/api'),
]


class _FakePaginator:
    def __init__(self, pages: list[dict[str, Any]], calls: list[dict[str, Any]]) -> None:
        self._pages = pages
        self._calls = calls

    def paginate(self, **kwargs: Any) -> Iterator[dict[str, Any]]:
        self._calls.append(kwargs)
        yield from self._pages


class _FakeLogsClient:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self._pages = pages
        self.paginate_calls: list[dict[str, Any]] = []

    def get_paginator(self, name: str) -> _FakePaginator:
        assert name == 'describe_log_groups'
        return _FakePaginator(self._pages, self.paginate_calls)


def _api_group(name: str, *, include_optional: bool = True) -> dict[str, Any]:
    group: dict[str, Any] = {
        'logGroupName': name,
        'arn': f'{ACCOUNT_ARN_PREFIX}{name}:*',
        'creationTime': int(CREATED.timestamp() * 1000),
    }
    if include_optional:
        group['storedBytes'] = 2048
        group['retentionInDays'] = 14
    return group


def test_describe_log_groups_paginates_across_pages():
    client = _FakeLogsClient(
        [
            {'logGroups': [_api_group('/aws/lambda/api'), _api_group('/aws/lambda/worker')]},
            {'logGroups': [_api_group('/ecs/api')]},
        ],
    )

    groups = list(describe_log_groups(client=client))

    assert _names(groups) == ['/aws/lambda/api', '/aws/lambda/worker', '/ecs/api']


def test_describe_log_groups_strips_trailing_wildcard_from_arn():
    client = _FakeLogsClient([{'logGroups': [_api_group('/aws/lambda/api')]}])

    groups = list(describe_log_groups(client=client))

    assert groups[0].arn == f'{ACCOUNT_ARN_PREFIX}/aws/lambda/api'


def test_describe_log_groups_prefers_unsuffixed_log_group_arn():
    raw = _api_group('/aws/lambda/api')
    raw['logGroupArn'] = f'{ACCOUNT_ARN_PREFIX}/aws/lambda/api'
    client = _FakeLogsClient([{'logGroups': [raw]}])

    groups = list(describe_log_groups(client=client))

    assert groups[0].arn == f'{ACCOUNT_ARN_PREFIX}/aws/lambda/api'


def test_describe_log_groups_converts_creation_time_from_epoch_ms():
    client = _FakeLogsClient([{'logGroups': [_api_group('/aws/lambda/api')]}])

    groups = list(describe_log_groups(client=client))

    assert groups[0].created == CREATED
    assert groups[0].created is not None
    assert groups[0].created.tzinfo == UTC


def test_describe_log_groups_tolerates_absent_optional_fields():
    raw = _api_group('/aws/lambda/api', include_optional=False)
    del raw['creationTime']
    client = _FakeLogsClient([{'logGroups': [raw]}])

    groups = list(describe_log_groups(client=client))

    assert groups[0].stored_bytes is None
    assert groups[0].retention_days is None
    assert groups[0].created is None


def test_describe_log_groups_forwards_prefix_only_when_given():
    client = _FakeLogsClient([{'logGroups': []}])

    list(describe_log_groups(client=client))
    list(describe_log_groups(prefix='/aws/lambda/', client=client))

    assert client.paginate_calls == [{}, {'logGroupNamePrefix': '/aws/lambda/'}]


def test_exact_match_returns_only_that_group():
    assert _names(resolve_group_pattern('/aws/lambda/api', GROUPS)) == ['/aws/lambda/api']


def test_prefix_match_when_no_exact_match():
    assert _names(resolve_group_pattern('/aws/lambda/api-', GROUPS)) == ['/aws/lambda/api-handler']


def test_prefix_match_is_case_sensitive():
    assert resolve_group_pattern('/AWS/LAMBDA/', GROUPS) == []


def test_prefix_match_preserves_input_order():
    reordered = [GROUPS[2], GROUPS[1], GROUPS[0]]

    assert _names(resolve_group_pattern('/aws/lambda/', reordered)) == [
        '/aws/lambda/worker',
        '/aws/lambda/api-handler',
        '/aws/lambda/api',
    ]


def test_glob_matches_across_path_separators():
    assert _names(resolve_group_pattern('*/api', GROUPS)) == ['/aws/lambda/api', '/ecs/api']


def test_glob_without_separator_matches_full_name():
    assert _names(resolve_group_pattern('*api*', GROUPS)) == [
        '/aws/lambda/api',
        '/aws/lambda/api-handler',
        '/ecs/api',
    ]


def test_glob_is_case_sensitive():
    assert resolve_group_pattern('*/API', GROUPS) == []


def test_glob_pattern_skips_exact_and_prefix_rungs():
    literal = _group('/aws/lambda/api*')
    groups = [literal, *GROUPS]

    assert _names(resolve_group_pattern('/aws/lambda/api*', groups)) == [
        '/aws/lambda/api*',
        '/aws/lambda/api',
        '/aws/lambda/api-handler',
    ]


def test_empty_pattern_returns_all_groups():
    assert _names(resolve_group_pattern('', GROUPS)) == _names(GROUPS)


def test_whitespace_pattern_returns_all_groups():
    assert _names(resolve_group_pattern('   ', GROUPS)) == _names(GROUPS)


def test_pattern_is_stripped_before_matching():
    assert _names(resolve_group_pattern('  /ecs/api  ', GROUPS)) == ['/ecs/api']


def test_no_match_returns_empty_list():
    assert resolve_group_pattern('/no/such/group', GROUPS) == []
    assert resolve_group_pattern('/no/such/*', GROUPS) == []


def test_empty_group_list_resolves_to_empty():
    assert resolve_group_pattern('/aws/lambda/api', []) == []
    assert resolve_group_pattern('', []) == []
