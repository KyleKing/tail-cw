"""Log group discovery and pattern resolution.

Wraps ``DescribeLogGroups`` as a stream of :class:`LogGroupInfo` records and
resolves a user-typed pattern against them without touching AWS, so the
resolution rules stay unit-testable.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from fnmatch import fnmatchcase
from typing import Any

from tail_cw.aws.client import _epoch_ms_to_datetime

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


async def describe_log_groups(
    client: Any,
    *,
    prefix: str | None = None,
) -> AsyncIterator[LogGroupInfo]:
    """Stream log groups from ``DescribeLogGroups``, paginating as needed.

    Args:
        client: An open CloudWatch Logs client, from :meth:`ClientPool.client`.
        prefix: Restrict results to this ``logGroupNamePrefix``.

    Yields:
        LogGroupInfo records in the order the API returns them.
    """
    kwargs: dict[str, Any] = {}
    if prefix is not None:
        kwargs['logGroupNamePrefix'] = prefix

    paginator = client.get_paginator('describe_log_groups')
    async for page in paginator.paginate(**kwargs):
        for group in page.get('logGroups', []):
            yield _to_log_group_info(group)


def _is_glob(pattern: str) -> bool:
    return any(char in GLOB_METACHARACTERS for char in pattern)


def resolve_group_pattern(pattern: str, groups: Sequence[LogGroupInfo]) -> list[LogGroupInfo]:
    """Resolve a pattern against known log groups, preserving input order.

    A pattern holding any of ``*?[`` is a glob by intent and is matched with
    case-sensitive ``fnmatch`` alone. Otherwise the ladder runs exact name (which
    wins by itself), then case-sensitive prefix, then case-sensitive substring,
    then case-folded substring, and stops at the first rung with a hit. The
    substring rungs matter because CloudWatch names are slash-heavy, so the part
    a reader remembers (``handler``) is rarely the part a name starts with. An
    empty or whitespace-only pattern returns every group.
    """
    stripped = pattern.strip()
    if not stripped:
        return list(groups)
    if _is_glob(stripped):
        return [group for group in groups if fnmatchcase(group.name, stripped)]
    for group in groups:
        if group.name == stripped:
            return [group]
    if prefixed := [group for group in groups if group.name.startswith(stripped)]:
        return prefixed
    if contained := [group for group in groups if stripped in group.name]:
        return contained
    folded = stripped.casefold()
    return [group for group in groups if folded in group.name.casefold()]
