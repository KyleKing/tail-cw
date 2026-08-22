"""Cover the log table's column budget and how it renders a record."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from rich.text import Text

from tail_cw.config import MessageConfig
from tail_cw.query.severity import Severity
from tail_cw.tui.log_viewer import (
    COMPACT_TIME_WIDTH,
    FULL_TIME_WIDTH,
    MIN_MESSAGE_WIDTH,
    SEVERITY_GLYPHS,
    format_log_event_detail,
    format_log_event_detail_with_json,
    format_row,
    format_rows,
    format_timestamp,
    message_text,
    parse_jsonl_message,
    plan_columns,
    shorten,
)
from tests.factories import make_event

CONFIG = MessageConfig()
MOMENT = datetime(2025, 1, 15, 10, 30, 45, 123000, tzinfo=UTC)


def _keys(width: int, *, single_group: bool = True) -> list[str]:
    return [column.key for column in plan_columns(width, single_group=single_group)]


def _width(width: int, key: str, *, single_group: bool = True) -> int:
    return next(column.width for column in plan_columns(width, single_group=single_group) if column.key == key)


@pytest.mark.parametrize(
    ('width', 'single_group', 'expected'),
    [
        (200, False, ['timestamp', 'severity', 'log_group', 'log_stream', 'message']),
        # One group means the group column repeats one value on every row.
        (200, True, ['timestamp', 'severity', 'log_stream', 'message']),
        # The detail pane carries the stream in full, so it is the first to go.
        (110, False, ['timestamp', 'severity', 'log_group', 'message']),
        (80, True, ['timestamp', 'severity', 'message']),
    ],
)
def test_columns_collapse_by_priority(width, single_group, expected):
    assert _keys(width, single_group=single_group) == expected


@pytest.mark.parametrize(('width', 'expected'), [(200, FULL_TIME_WIDTH), (80, COMPACT_TIME_WIDTH)])
def test_the_date_goes_before_the_message_does(width, expected):
    assert _width(width, 'timestamp') == expected


def test_the_message_column_gets_what_is_left_rather_than_twelve_characters():
    """The 80-column view used to render `{"method":"G` and nothing else."""
    assert _width(80, 'message') >= 60
    assert _width(200, 'message') > _width(120, 'message')


def test_a_terminal_too_narrow_to_budget_still_leaves_a_readable_message():
    assert _width(30, 'message') == MIN_MESSAGE_WIDTH


@pytest.mark.parametrize(
    ('width', 'expected'),
    [(FULL_TIME_WIDTH, '2025-01-15 10:30:45.123'), (COMPACT_TIME_WIDTH, '10:30:45.123')],
)
def test_format_timestamp(width, expected):
    rendered = format_timestamp(MOMENT, width=width)

    assert rendered.plain == expected
    assert 'cyan' in str(rendered.style)


def test_a_record_reads_as_its_phrase_then_its_fields():
    event = make_event(json.dumps({'event': 'downstream call failed', 'service': 'payments', 'attempt': 2}))

    rendered = message_text(event, CONFIG)

    assert rendered.plain == 'downstream call failed service=payments attempt=2'


def test_fields_the_table_already_shows_are_dropped_from_the_remainder():
    event = make_event(json.dumps({'timestamp': '2026-08-21T00:00:00Z', 'level': 'error', 'event': 'boom'}))

    assert message_text(event, CONFIG).plain == 'boom'


def test_a_record_with_no_phrase_field_still_shows_its_fields():
    event = make_event(json.dumps({'route': '/pay', 'status_code': 502}))

    assert message_text(event, CONFIG).plain == 'route=/pay status_code=502'


def test_a_nested_value_renders_as_compact_json():
    event = make_event(json.dumps({'event': 'call', 'headers': {'x': 1}}))

    assert message_text(event, CONFIG).plain == 'call headers={"x":1}'


def test_a_null_field_is_not_shown_as_none():
    """Polars widens the parsed struct across a file, so absent fields arrive as null."""
    event = make_event(json.dumps({'event': 'call', 'error_type': None}))

    assert message_text(event, CONFIG).plain == 'call'


def test_a_plain_line_loses_the_timestamp_the_column_already_shows():
    event = make_event('2026-08-21 19:55:13 UTC:10.0.78.100 duplicate key value')

    assert message_text(event, CONFIG).plain.startswith('UTC:10.0.78.100')


def test_a_long_message_is_cut_with_an_ellipsis_rather_than_clipped():
    event = make_event(json.dumps({'event': 'x' * 200}))

    rendered = message_text(event, CONFIG, width=40)

    assert len(rendered.plain) == 40
    assert rendered.plain.endswith('…')


@pytest.mark.parametrize(
    ('message', 'severity', 'explicit'),
    [
        ('{"level":"error","event":"boom"}', Severity.ERROR, True),
        ('{"event":"fine","status_code":502}', Severity.ERROR, True),
        ('{"level":"warning","event":"retry"}', Severity.WARNING, True),
        ('2026-08-21 ERROR:  duplicate key', Severity.ERROR, False),
        ('{"event":"fine"}', Severity.INFO, True),
    ],
)
def test_severity_shows_as_a_glyph_and_dims_when_it_was_inferred(message, severity, explicit):
    columns = plan_columns(200, single_group=True)
    cells = format_row(make_event(message), columns, CONFIG)
    glyph = cells[[column.key for column in columns].index('severity')]

    assert isinstance(glyph, Text)
    assert glyph.plain == SEVERITY_GLYPHS[severity], 'the glyph carries severity where NO_COLOR loses the colour'
    if severity is not Severity.INFO:
        assert ('dim' in str(glyph.style)) is not explicit


def test_a_row_has_one_cell_per_planned_column():
    columns = plan_columns(80, single_group=True)

    assert len(format_row(make_event('hi'), columns, CONFIG)) == len(columns)
    assert format_rows([make_event('a'), make_event('b')], columns, CONFIG) != []


def test_a_clipped_name_never_reads_as_a_whole_name():
    """`/aws/rds/cluster/irm` is a plausible group name that does not exist."""
    assert shorten('/aws/rds/cluster/irm-prod-hatchet-cluster/postgresql', 20) == '/aws/rds/cluster/ir…'
    assert shorten('/short', 20) == '/short'


def test_the_detail_pane_shows_every_field_and_the_message_in_full():
    event = make_event('{"key": "value"}')

    detail = format_log_event_detail(event)

    assert 'Log Group: /aws/test/group' in detail
    assert '{"key": "value"}' in detail
    assert 'Message (parsed JSON)' in format_log_event_detail_with_json(event)


def test_the_detail_pane_leaves_a_plain_line_alone():
    detail = format_log_event_detail_with_json(make_event('not json'))

    assert 'parsed JSON' not in detail
    assert parse_jsonl_message('not json') is None
