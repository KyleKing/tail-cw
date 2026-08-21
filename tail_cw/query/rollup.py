"""Roll log events up into recurring patterns counted by severity, time bucket, and group.

One pass over the events, keyed by the message body's shape (see
:func:`~tail_cw.query.patterns.message_shape_key`), then an optional fuzzy pass that folds
shapes differing only in a literal phrase. Memory is bounded by the number of distinct
shapes rather than the number of events. Pure: no AWS calls, no I/O.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from tail_cw.aws.client import LogEvent
from tail_cw.query.fuzzy import DEFAULT_SIMILARITY, merge_similar_keys
from tail_cw.query.patterns import message_shape_key
from tail_cw.query.severity import Severity, event_severity

DEFAULT_PATTERN_LIMIT = 20
DEFAULT_EXAMPLE_CHARS = 240


class Granularity(StrEnum):
    """Time bucket width for the per-period counts."""

    HOUR = 'hour'
    DAY = 'day'


@dataclass(frozen=True)
class PatternRollup:
    """One recurring message shape with its counts across severity, time, and group."""

    key: str
    example: str
    severity: Severity
    count: int
    first_seen: datetime
    last_seen: datetime
    log_groups: tuple[tuple[str, int], ...]
    buckets: tuple[tuple[str, int], ...]
    merged_shapes: int = 1


@dataclass(frozen=True)
class RollupReport:
    """Ranked patterns plus the totals needed to say what was left out."""

    patterns: tuple[PatternRollup, ...]
    granularity: Granularity
    bucket_labels: tuple[str, ...]
    scanned: int
    matched: int
    severity_totals: tuple[tuple[Severity, int], ...]
    distinct_shapes: int
    distinct_patterns: int


@dataclass
class _Accumulator:
    """Mutable per-shape tally, collapsed into a :class:`PatternRollup` at the end."""

    example: str
    severity: Severity
    first_seen: datetime
    last_seen: datetime
    count: int = 0
    shapes: int = 1
    log_groups: Counter[str] = field(default_factory=Counter)
    buckets: Counter[str] = field(default_factory=Counter)

    def absorb(self, other: _Accumulator) -> None:
        self.count += other.count
        self.shapes += other.shapes
        self.severity = max(self.severity, other.severity)
        self.first_seen = min(self.first_seen, other.first_seen)
        self.last_seen = max(self.last_seen, other.last_seen)
        self.log_groups.update(other.log_groups)
        self.buckets.update(other.buckets)


def bucket_label(moment: datetime, granularity: Granularity) -> str:
    """Return the time bucket a moment falls in, as a sortable label."""
    if granularity is Granularity.DAY:
        return moment.strftime('%Y-%m-%d')
    return moment.strftime('%Y-%m-%dT%H:00Z')


def bucket_labels_for_window(start: datetime, end: datetime, granularity: Granularity) -> tuple[str, ...]:
    """Return every bucket label in a window, including those with no events.

    An empty bucket is a finding, so the caller can distinguish "nothing happened" from
    "we did not look".
    """
    step = timedelta(days=1) if granularity is Granularity.DAY else timedelta(hours=1)
    labels: list[str] = []
    cursor = _floor(start, granularity)
    while cursor <= end:
        labels.append(bucket_label(cursor, granularity))
        cursor += step
    return tuple(labels)


def roll_up(
    events: Iterable[LogEvent],
    *,
    window: tuple[datetime, datetime] | None = None,
    granularity: Granularity = Granularity.HOUR,
    min_severity: Severity = Severity.WARNING,
    limit: int = DEFAULT_PATTERN_LIMIT,
    example_chars: int = DEFAULT_EXAMPLE_CHARS,
    similarity: float | None = DEFAULT_SIMILARITY,
) -> RollupReport:
    """Count message shapes at or above `min_severity`, ranked by frequency.

    Ties break by first appearance so a repeated run of the report keeps a stable order.
    Passing `window` labels every bucket in the range, so periods with no events render as
    zeros rather than vanishing. `similarity` of None skips the fuzzy merge.
    """
    tallies, severity_totals, scanned, matched = _tally(events, min_severity=min_severity, granularity=granularity)
    distinct_shapes = len(tallies)
    ranked = _rank(tallies)
    if similarity is not None:
        tallies = _merge(tallies, ranked, similarity=similarity)
        ranked = _rank(tallies)

    return RollupReport(
        patterns=tuple(_to_pattern(key, tallies[key], example_chars=example_chars) for key in ranked[:limit]),
        granularity=granularity,
        bucket_labels=bucket_labels_for_window(*window, granularity) if window else (),
        scanned=scanned,
        matched=matched,
        severity_totals=tuple(sorted(severity_totals.items(), reverse=True)),
        distinct_shapes=distinct_shapes,
        distinct_patterns=len(tallies),
    )


def _tally(
    events: Iterable[LogEvent],
    *,
    min_severity: Severity,
    granularity: Granularity,
) -> tuple[dict[str, _Accumulator], Counter[Severity], int, int]:
    tallies: dict[str, _Accumulator] = {}
    severity_totals: Counter[Severity] = Counter()
    scanned = 0
    matched = 0
    for event in events:
        scanned += 1
        severity = event_severity(event)
        if severity < min_severity:
            continue
        matched += 1
        severity_totals[severity] += 1
        key = message_shape_key(event.message)
        tally = tallies.get(key)
        if tally is None:
            tally = _Accumulator(
                example=event.message,
                severity=severity,
                first_seen=event.timestamp,
                last_seen=event.timestamp,
            )
            tallies[key] = tally
        tally.count += 1
        tally.severity = max(tally.severity, severity)
        tally.first_seen = min(tally.first_seen, event.timestamp)
        tally.last_seen = max(tally.last_seen, event.timestamp)
        tally.log_groups[event.log_group] += 1
        tally.buckets[bucket_label(event.timestamp, granularity)] += 1
    return tallies, severity_totals, scanned, matched


def _rank(tallies: dict[str, _Accumulator]) -> list[str]:
    appearance = {key: index for index, key in enumerate(tallies)}
    return sorted(tallies, key=lambda key: (-tallies[key].count, appearance[key]))


def _merge(tallies: dict[str, _Accumulator], ranked: list[str], *, similarity: float) -> dict[str, _Accumulator]:
    merged: dict[str, _Accumulator] = {}
    for cluster in merge_similar_keys(ranked, similarity=similarity):
        head, *rest = cluster.members
        tally = tallies[head]
        for member in rest:
            tally.absorb(tallies[member])
        merged[cluster.key] = tally
    return merged


def _to_pattern(key: str, tally: _Accumulator, *, example_chars: int) -> PatternRollup:
    return PatternRollup(
        key=key,
        example=_truncate(tally.example, example_chars),
        severity=tally.severity,
        count=tally.count,
        first_seen=tally.first_seen,
        last_seen=tally.last_seen,
        log_groups=tuple(tally.log_groups.most_common()),
        buckets=tuple(sorted(tally.buckets.items())),
        merged_shapes=tally.shapes,
    )


def _floor(moment: datetime, granularity: Granularity) -> datetime:
    if granularity is Granularity.DAY:
        return moment.replace(hour=0, minute=0, second=0, microsecond=0)
    return moment.replace(minute=0, second=0, microsecond=0)


def _truncate(text: str, limit: int) -> str:
    collapsed = ' '.join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + '…'
