"""Bucket the events a view is showing, so a spike or a gap is visible before scrolling.

Separate from :mod:`tail_cw.preview`, whose bucketing answers "when was this group busy"
over a whole window from a sample. This one answers "when did *these* events happen",
where "these" is whatever the current search left on screen, and it carries severity per
bucket because a burst of errors and a burst of traffic want different colours.

One bucket per character column, so nothing is resampled after the fact.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from tail_cw.aws.events import LogEvent
from tail_cw.query.severity import Severity, event_severity


@dataclass(frozen=True)
class HistogramBucket:
    """One column of the histogram.

    Attributes:
        start: When the bucket opens.
        count: Events that landed in it.
        severity: Highest severity among them, INFO when empty.
    """

    start: datetime
    count: int
    severity: Severity


def bucket_events(
    events: Iterable[LogEvent],
    *,
    start: datetime,
    end: datetime,
    columns: int,
) -> list[HistogramBucket]:
    """Bucket events into ``columns`` equal spans, empty buckets included.

    An empty bucket is a finding rather than a gap to skip, so the list is always
    ``columns`` long. Returns nothing when the window or the column count is empty,
    which lets a caller render nothing without a special case.
    """
    span = (end - start).total_seconds()
    if columns <= 0 or span <= 0:
        return []
    width = span / columns
    counts = [0] * columns
    worst = [Severity.INFO] * columns
    for event in events:
        offset = (event.timestamp - start).total_seconds()
        if offset < 0 or offset > span:
            continue
        index = min(int(offset / span * columns), columns - 1)
        counts[index] += 1
        worst[index] = max(worst[index], event_severity(event))
    return [
        HistogramBucket(start=start + timedelta(seconds=width * index), count=count, severity=severity)
        for index, (count, severity) in enumerate(zip(counts, worst, strict=True))
    ]


def peak_bucket(buckets: Sequence[HistogramBucket]) -> HistogramBucket | None:
    """The busiest bucket, earliest first on a tie, or None when nothing landed."""
    populated = [bucket for bucket in buckets if bucket.count]
    if not populated:
        return None
    return max(populated, key=lambda bucket: bucket.count)


def histogram_headline(buckets: Sequence[HistogramBucket]) -> str:
    """One line naming the peak and how uneven the distribution is.

    The ratio is the point: an even spread and a single spike carry the same total, and
    only one of them is a lead.
    """
    peak = peak_bucket(buckets)
    if peak is None:
        return 'no events in the window'
    total = sum(bucket.count for bucket in buckets)
    average = total / len(buckets)
    quiet = sum(1 for bucket in buckets if not bucket.count)
    shape = f'{peak.count / average:.0f}x average' if average else 'flat'
    return f'peak {peak.count} at {peak.start:%H:%M:%S}, {shape}, {quiet}/{len(buckets)} quiet'
