"""Tests for the pattern rollup, its fuzzy merge, and the markdown report."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from tail_cw.aws.client import LogEvent
from tail_cw.query.fuzzy import VARIABLE_PLACEHOLDER, merge_similar_keys
from tail_cw.query.report import render_markdown
from tail_cw.query.rollup import Granularity, bucket_labels_for_window, roll_up
from tail_cw.query.severity import Severity

START = datetime(2026, 8, 21, 17, 0, tzinfo=UTC)
END = START + timedelta(hours=3)

_CUSTOM_FIELDS = ('Solution Type', 'UK-Based Contractor', 'Restricted Data Volume')


def _event(payload: dict[str, object] | str, *, offset: timedelta, log_group: str = '/g') -> LogEvent:
    message = payload if isinstance(payload, str) else json.dumps(payload)
    return LogEvent(
        log_group=log_group,
        log_stream='s',
        timestamp=START + offset,
        message=message,
        event_id=f'e{offset}',
        ingestion_time=None,
    )


def _warning(text: str, *, offset: timedelta, log_group: str = '/g') -> LogEvent:
    return _event(
        {'level': 'warning', 'logger': 'import_export.action', 'event': text},
        offset=offset,
        log_group=log_group,
    )


def test_roll_up_counts_by_pattern_group_and_bucket():
    events = [
        _warning('disk nearly full at 91%', offset=timedelta(minutes=5)),
        _warning('disk nearly full at 93%', offset=timedelta(hours=2, minutes=5), log_group='/other'),
        _event({'level': 'info', 'event': 'fine'}, offset=timedelta(minutes=6)),
    ]

    report = roll_up(events, window=(START, END), granularity=Granularity.HOUR)

    assert report.scanned == 3
    assert report.matched == 2
    assert report.severity_totals == ((Severity.WARNING, 2),)
    pattern = report.patterns[0]
    assert pattern.count == 2
    assert pattern.log_groups == (('/g', 1), ('/other', 1))
    assert pattern.first_seen == START + timedelta(minutes=5)
    assert pattern.last_seen == START + timedelta(hours=2, minutes=5)
    # The middle hour had none, and the window makes that visible rather than absent.
    assert report.bucket_labels == ('2026-08-21T17:00Z', '2026-08-21T18:00Z', '2026-08-21T19:00Z')
    assert dict(pattern.buckets) == {'2026-08-21T17:00Z': 1, '2026-08-21T19:00Z': 1}


def test_roll_up_respects_the_minimum_severity():
    events = [
        _warning('slow', offset=timedelta(minutes=1)),
        _event({'level': 'error', 'event': 'boom'}, offset=timedelta(minutes=2)),
    ]

    assert roll_up(events, min_severity=Severity.ERROR).matched == 1
    assert roll_up(events, min_severity=Severity.WARNING).matched == 2
    assert roll_up(events, min_severity=Severity.INFO).matched == 2


def test_roll_up_merges_shapes_differing_only_in_a_literal_phrase():
    events = [
        _warning(f'Skipping upsert for legacy custom field {name}: value is not valid', offset=timedelta(minutes=index))
        for index, name in enumerate(_CUSTOM_FIELDS)
    ]

    merged = roll_up(events)
    unmerged = roll_up(events, similarity=None)

    assert unmerged.distinct_patterns == len(_CUSTOM_FIELDS)
    assert merged.distinct_shapes == len(_CUSTOM_FIELDS)
    assert merged.distinct_patterns == 1
    assert merged.patterns[0].count == len(_CUSTOM_FIELDS)
    assert merged.patterns[0].merged_shapes == len(_CUSTOM_FIELDS)
    assert VARIABLE_PLACEHOLDER in merged.patterns[0].key


def test_roll_up_keeps_genuinely_different_messages_apart():
    events = [
        _warning('disk nearly full', offset=timedelta(minutes=1)),
        _warning('upstream vendor lookup returned nothing at all', offset=timedelta(minutes=2)),
    ]

    assert roll_up(events).distinct_patterns == 2


def test_merge_similar_keys_leaves_keys_past_the_cap_unmerged():
    keys = [f'Skipping upsert for legacy custom field {name}' for name in 'abcdef']

    clusters = merge_similar_keys(keys, max_keys=2)

    assert len(clusters) == 1 + 4
    assert clusters[0].members == (keys[0], keys[1])
    assert [cluster.key for cluster in clusters[1:]] == keys[2:]


@pytest.mark.parametrize(
    ('granularity', 'expected'),
    [
        (Granularity.DAY, ('2026-08-21',)),
        (Granularity.HOUR, ('2026-08-21T17:00Z',)),
    ],
)
def test_bucket_labels_for_window_covers_the_whole_range(granularity, expected):
    assert bucket_labels_for_window(START, START + timedelta(hours=1), granularity) == expected


def test_bucket_labels_for_window_includes_a_partly_covered_bucket():
    labels = bucket_labels_for_window(START, START + timedelta(hours=1, minutes=30), Granularity.HOUR)

    assert labels == ('2026-08-21T17:00Z', '2026-08-21T18:00Z')


def test_render_markdown_reports_totals_and_what_was_left_out():
    texts = ('disk nearly full', 'vendor lookup empty', 'hatchet token missing', 'risk scale ambiguous')
    events = [_warning(text, offset=timedelta(minutes=index)) for index, text in enumerate(texts)]

    document = render_markdown(
        roll_up(events, window=(START, END), limit=1, similarity=None),
        title='Warnings',
        window_label='w',
        source='s',
    )

    assert document.startswith('# Warnings')
    assert 'Scanned 4 events, 4 matched (4 warning) in 4 distinct shapes.' in document
    assert 'Top 1 shown, 3 not listed.' in document
    assert document.count('### ') == 1


def test_render_markdown_says_so_when_nothing_matched():
    document = render_markdown(roll_up([]), title='Warnings', window_label='w', source='s')

    assert 'No events at or above the requested severity.' in document
