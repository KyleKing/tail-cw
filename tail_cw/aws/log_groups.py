"""Log group discovery and pattern resolution.

Wraps ``DescribeLogGroups`` as a stream of :class:`LogGroupInfo` records and
resolves a user-typed pattern against them without touching AWS, so the
resolution rules stay unit-testable.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from fnmatch import fnmatchcase
from typing import Any

from tail_cw.aws.client import _epoch_ms_to_datetime, build_client

GLOB_METACHARACTERS = frozenset('*?[')
"""Characters that mark a pattern as a glob rather than a name or prefix."""


@dataclass(frozen=True)
class LogGroupInfo:
    """Metadata for one CloudWatch log group.

    Attributes:
        name: The log group name.
        arn: Log group ARN without the trailing ``:*`` that ``DescribeLogGroups``
            appends, so it is accepted by ``logGroupIdentifiers``.
        stored_bytes: Bytes stored, or None when the API omits the field.
        retention_days: Retention in days, or None when the group never expires.
        created: Creation time (UTC), or None when the API omits the field.
    """

    name: str
    arn: str
    stored_bytes: int | None
    retention_days: int | None
    created: datetime | None


def _to_log_group_info(group: dict[str, Any]) -> LogGroupInfo:
    created_ms = group.get('creationTime')
    return LogGroupInfo(
        name=group['logGroupName'],
        arn=(group.get('logGroupArn') or group.get('arn', '')).removesuffix(':*'),
        stored_bytes=group.get('storedBytes'),
        retention_days=group.get('retentionInDays'),
        created=_epoch_ms_to_datetime(created_ms) if created_ms is not None else None,
    )


def describe_log_groups(
    *,
    prefix: str | None = None,
    profile_name: str | None = None,
    region_name: str | None = None,
    client: Any | None = None,
) -> Iterator[LogGroupInfo]:
    """Stream log groups from ``DescribeLogGroups``, paginating as needed.

    A boto3 CloudWatch Logs client may be injected for testing; otherwise one is
    built from the profile and region.

    Yields:
        LogGroupInfo records in the order the API returns them.
    """
    logs = client if client is not None else build_client('logs', region_name=region_name, profile_name=profile_name)
    kwargs: dict[str, Any] = {}
    if prefix is not None:
        kwargs['logGroupNamePrefix'] = prefix

    paginator = logs.get_paginator('describe_log_groups')
    for page in paginator.paginate(**kwargs):
        for group in page.get('logGroups', []):
            yield _to_log_group_info(group)


def _is_glob(pattern: str) -> bool:
    return any(char in GLOB_METACHARACTERS for char in pattern)


def resolve_group_pattern(pattern: str, groups: Sequence[LogGroupInfo]) -> list[LogGroupInfo]:
    """Resolve a pattern against known log groups, preserving input order.

    A pattern holding any of ``*?[`` is a glob by intent and is matched with
    case-sensitive ``fnmatch`` alone. Otherwise the ladder runs exact name (which
    wins by itself), then case-sensitive prefix, and stops at the first rung with
    a hit. An empty or whitespace-only pattern returns every group.
    """
    stripped = pattern.strip()
    if not stripped:
        return list(groups)
    if _is_glob(stripped):
        return [group for group in groups if fnmatchcase(group.name, stripped)]
    for group in groups:
        if group.name == stripped:
            return [group]
    return [group for group in groups if group.name.startswith(stripped)]
