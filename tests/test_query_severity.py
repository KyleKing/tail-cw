"""Tests for event severity classification."""

import json
from datetime import UTC, datetime

import pytest

from tail_cw.aws.client import LogEvent
from tail_cw.query.severity import Severity, event_severity, keyword_severity

NOW = datetime(2026, 8, 21, 17, 0, tzinfo=UTC)


def _event(message: str) -> LogEvent:
    return LogEvent(
        log_group='/g',
        log_stream='s',
        timestamp=NOW,
        message=message,
        event_id='e',
        ingestion_time=None,
    )


@pytest.mark.parametrize(
    ('payload', 'expected'),
    [
        ({'level': 'error', 'event': 'boom'}, Severity.ERROR),
        ({'level': 'warning', 'event': 'slow'}, Severity.WARNING),
        ({'level': 'info', 'event': 'fine'}, Severity.INFO),
        ({'severity': 'CRITICAL', 'event': 'boom'}, Severity.ERROR),
        ({'status_code': 503, 'event': 'http_request'}, Severity.ERROR),
        ({'status_code': 404, 'event': 'http_request'}, Severity.WARNING),
        ({'status_code': 200, 'event': 'http_request'}, Severity.INFO),
        # A structured record is judged on its fields, so an id that reads like a level
        # cannot promote it.
        ({'level': 'info', 'event': 'trace-error lookup'}, Severity.INFO),
        # The highest signal across fields wins, whichever field carries it.
        ({'level': 'warning', 'status_code': 500, 'event': 'x'}, Severity.ERROR),
        ({'level': 'info', 'message': 'unhandled exception'}, Severity.ERROR),
    ],
)
def test_event_severity_reads_structured_fields(payload, expected):
    assert event_severity(_event(json.dumps(payload))) is expected


@pytest.mark.parametrize(
    ('message', 'expected'),
    [
        ('WARNING: Bedrock transient error', Severity.WARNING),
        ('[WARNING] something failed', Severity.WARNING),
        ('WARN - retrying after error', Severity.WARNING),
        ('ERROR: boom', Severity.ERROR),
        ('INFO: all good', Severity.INFO),
        ('Unhandled exception in worker', Severity.ERROR),
        ('request completed', Severity.INFO),
    ],
)
def test_keyword_severity_prefers_a_self_declared_level(message, expected):
    assert keyword_severity(message) is expected
    assert event_severity(_event(message)) is expected


def test_event_severity_falls_back_to_keywords_for_non_json():
    assert event_severity(_event('not json {oops} fatal')) is Severity.ERROR
