"""Tests for splitting a requested window into cacheable segments."""

import itertools
from datetime import UTC, datetime, timedelta

import pytest

from tail_cw.cache.window import INGESTION_LAG, TRANSIENT_TTL_SECONDS, plan_segments, segment_width

_NOW = datetime(2026, 8, 21, 18, 15, tzinfo=UTC)


@pytest.mark.parametrize(
    ('window', 'expected'),
    [
        (timedelta(minutes=1), timedelta(minutes=5)),
        (timedelta(hours=1), timedelta(minutes=5)),
        (timedelta(hours=3), timedelta(hours=1)),
        (timedelta(days=1), timedelta(hours=1)),
        (timedelta(days=7), timedelta(days=1)),
    ],
)
def test_segment_width_grows_with_the_window(window, expected):
    assert segment_width(window) == expected


def test_segments_cover_the_window_exactly():
    start = _NOW - timedelta(hours=1) - timedelta(seconds=33)
    segments = plan_segments(start, _NOW, now=_NOW)

    assert segments[0].start == start
    assert segments[-1].end == _NOW
    assert all(left.end == right.start for left, right in itertools.pairwise(segments))


def test_a_relative_window_reuses_its_interior_as_now_moves():
    """The measured defect: two ``--start 1h`` runs shared no cached work at all."""
    first = plan_segments(_NOW - timedelta(hours=1), _NOW, now=_NOW)
    later = _NOW + timedelta(minutes=2)
    second = plan_segments(later - timedelta(hours=1), later, now=later)

    def durable(segments):
        return {(segment.start, segment.end) for segment in segments if segment.durable}

    assert len(durable(first) & durable(second)) >= 10


def test_only_aligned_segments_are_worth_keeping():
    start = _NOW - timedelta(hours=1) + timedelta(minutes=2)
    segments = plan_segments(start, _NOW, now=_NOW)

    assert segments[0].aligned is False
    assert segments[0].ttl_seconds == TRANSIENT_TTL_SECONDS
    assert all(segment.aligned for segment in segments[1:])


def test_a_segment_inside_the_ingestion_window_is_not_trusted():
    segments = plan_segments(_NOW - timedelta(hours=1), _NOW, now=_NOW)
    unsettled = [segment for segment in segments if not segment.settled]

    assert unsettled, 'the segment ending at now cannot have finished ingesting'
    assert all(segment.end > _NOW - INGESTION_LAG for segment in unsettled)
    assert all(segment.ttl_seconds == TRANSIENT_TTL_SECONDS for segment in unsettled)
    assert all(segment.durable for segment in segments if segment.end <= _NOW - INGESTION_LAG)


def test_a_window_shorter_than_one_boundary_stays_whole():
    start = _NOW - timedelta(minutes=2)

    assert plan_segments(start, _NOW, now=_NOW) == plan_segments(start, _NOW, now=_NOW)[:1]


def test_an_empty_window_is_rejected():
    with pytest.raises(ValueError, match='must be after start'):
        plan_segments(_NOW, _NOW, now=_NOW)
