"""Split a requested time window into cacheable segments.

A cache key holding the caller's exact microsecond window never collides with
the next one, so ``--start 1h`` refetched everything every time. Splitting the
window at aligned boundaries makes the interior of the window reusable: the
ragged ends move as ``now`` moves, the aligned segments do not.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

INGESTION_LAG = timedelta(minutes=5)
"""How close to now a window's end may be before its contents are untrustworthy.

CloudWatch keeps accepting events for a timestamp after that timestamp has
passed, so a window ending inside this margin is permanently short of events
that arrived seconds later.
"""

TRANSIENT_TTL_SECONDS = 900.0
"""Lifetime for a segment that must not become authoritative.

A ragged end is keyed to one request and is never asked for again, and an
unsettled segment is short. Both are written so the current command can read
them, then reclaimed rather than kept.
"""

_SEGMENT_WIDTHS = (
    (timedelta(hours=1), timedelta(minutes=5)),
    (timedelta(days=1), timedelta(hours=1)),
)
_WIDEST_SEGMENT = timedelta(days=1)


@dataclass(frozen=True)
class Segment:
    """One piece of a requested window, and whether the cache may keep it.

    Attributes:
        start: Segment start (inclusive).
        end: Segment end (exclusive), matching FilterLogEvents.
        aligned: True when the segment sits on a boundary another request would
            also ask for, which is what makes it worth keeping.
        settled: True when the segment ended long enough ago that CloudWatch is
            done ingesting into it. An unsettled segment is refetched even on a
            cache hit.
    """

    start: datetime
    end: datetime
    aligned: bool
    settled: bool

    @property
    def durable(self) -> bool:
        """True when this segment may be cached without an expiry."""
        return self.aligned and self.settled

    @property
    def ttl_seconds(self) -> float | None:
        """TTL to write this segment under; None means no expiration."""
        return None if self.durable else TRANSIENT_TTL_SECONDS


def segment_width(window: timedelta) -> timedelta:
    """Return the boundary spacing used for a window of this length.

    Widths grow with the window so the number of segments stays bounded: at most
    twelve for an hour, twenty-four for a day, one per day beyond that.
    """
    for limit, width in _SEGMENT_WIDTHS:
        if window <= limit:
            return width
    return _WIDEST_SEGMENT


def plan_segments(start: datetime, end: datetime, *, now: datetime) -> list[Segment]:
    """Split ``[start, end)`` into aligned segments plus the ragged ends.

    The caller's window is never widened or rounded; a request for 17:15 to 18:15
    still covers exactly that. Only the pieces that fall on aligned boundaries
    are worth caching, and only the ones that have settled are trusted on a
    later read.

    Args:
        start: Requested start (inclusive).
        end: Requested end (exclusive).
        now: Current time, used to decide which segments have settled.

    Returns:
        Segments in ascending order covering the window exactly, contiguous and
        non-overlapping. A window shorter than one boundary spacing yields a
        single unaligned segment.

    Raises:
        ValueError: If ``end`` is not after ``start``.

    Example:
        >>> from datetime import datetime, timedelta, UTC
        >>> now = datetime(2026, 8, 21, 18, 15, tzinfo=UTC)
        >>> plan = plan_segments(now - timedelta(hours=1), now, now=now)
        >>> len(plan), plan[0].aligned, plan[-1].settled
        (12, True, False)
    """
    if end <= start:
        msg = f'Cache window end ({end.isoformat()}) must be after start ({start.isoformat()})'
        raise ValueError(msg)

    width = segment_width(end - start)
    first = _next_boundary(start, width)
    last = _previous_boundary(end, width)
    settled_before = now - INGESTION_LAG

    def segment(piece_start: datetime, piece_end: datetime, *, aligned: bool) -> Segment:
        return Segment(piece_start, piece_end, aligned=aligned, settled=piece_end <= settled_before)

    if last <= first:
        return [segment(start, end, aligned=False)]

    segments = [segment(start, first, aligned=False)] if start < first else []
    boundary = first
    while boundary < last:
        segments.append(segment(boundary, boundary + width, aligned=True))
        boundary += width
    if last < end:
        segments.append(segment(last, end, aligned=False))
    return segments


def _next_boundary(moment: datetime, width: timedelta) -> datetime:
    floor = _previous_boundary(moment, width)
    return floor if floor == moment else floor + width


def _previous_boundary(moment: datetime, width: timedelta) -> datetime:
    elapsed = moment.astimezone(UTC) - _EPOCH
    return _EPOCH + width * (elapsed // width)
