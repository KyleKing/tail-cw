"""Cover the bucketing behind the log view's ``h`` row."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tail_cw.aws.events import LogEvent
from tail_cw.histogram import bucket_events, histogram_headline, peak_bucket
from tail_cw.query.severity import Severity

START = datetime(2026, 8, 22, 12, 0, 0, tzinfo=UTC)
END = START + timedelta(minutes=10)


def _event(offset_seconds: float, message: str = 'ok') -> LogEvent:
    return LogEvent(
        timestamp=START + timedelta(seconds=offset_seconds),
        message=message,
        log_group='/aws/test/group',
        log_stream='s',
        ingestion_time=None,
    )


def test_every_column_is_reported_including_the_empty_ones() -> None:
    """An empty bucket is a finding, not a gap to skip."""
    buckets = bucket_events([_event(0), _event(1), _event(599)], start=START, end=END, columns=10)

    assert len(buckets) == 10
    assert [bucket.count for bucket in buckets] == [2, 0, 0, 0, 0, 0, 0, 0, 0, 1]
    assert buckets[0].start == START
    assert buckets[9].start == START + timedelta(minutes=9)


def test_the_window_edges_land_inside_it() -> None:
    buckets = bucket_events([_event(0), _event(600), _event(-1), _event(601)], start=START, end=END, columns=4)

    assert [bucket.count for bucket in buckets] == [1, 0, 0, 1], 'end lands in the last bucket, outside is dropped'


def test_a_column_carries_the_worst_severity_in_it() -> None:
    """A burst of errors and a burst of traffic are the same height and not the same finding."""
    events = [_event(0, 'fine'), _event(1, 'request failed with ERROR'), _event(500, 'WARNING slow')]

    buckets = bucket_events(events, start=START, end=END, columns=10)

    assert buckets[0].severity is Severity.ERROR
    assert buckets[8].severity is Severity.WARNING
    assert buckets[5].severity is Severity.INFO, 'an empty column claims nothing'


def test_an_empty_window_or_no_columns_renders_nothing() -> None:
    assert bucket_events([_event(0)], start=START, end=START, columns=10) == []
    assert bucket_events([_event(0)], start=START, end=END, columns=0) == []
    assert peak_bucket([]) is None


def test_the_headline_names_the_spike_rather_than_the_total() -> None:
    """An even spread and a single spike carry the same total, and only one is a lead."""
    spike = bucket_events([_event(offset) for offset in range(60)], start=START, end=END, columns=10)
    even = bucket_events([_event(offset * 10) for offset in range(60)], start=START, end=END, columns=10)

    assert histogram_headline(spike) == 'peak 60 from 12:00:00, 10x average, 9/10 quiet'
    assert histogram_headline(even) == 'peak 6 from 12:00:00, 1x average, 0/10 quiet'
    assert histogram_headline(bucket_events([], start=START, end=END, columns=10)) == 'no events in the window'
    assert histogram_headline([]) == 'no events in the window'
