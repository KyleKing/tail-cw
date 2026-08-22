"""Cover the response parsing in :mod:`tail_cw.aws.alarms`."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tail_cw.aws.alarms import _to_alarm_summary


def test_a_summary_reports_utc_whatever_zone_botocore_parsed_it_in() -> None:
    """Every response timestamp arrives stamped with the machine's local zone, from botocore."""
    moment = datetime(2026, 8, 21, 13, 42, tzinfo=timezone(timedelta(hours=-5)))

    summary = _to_alarm_summary({'AlarmName': 'api-latency', 'StateUpdatedTimestamp': moment})

    assert summary.state_updated is not None
    assert summary.state_updated.utcoffset() == timedelta(0)
    assert summary.state_updated == moment, 'the instant must not move, only the zone it prints in'


def test_a_summary_survives_the_fields_the_api_omits() -> None:
    summary = _to_alarm_summary({'AlarmName': 'api-latency'})

    assert summary.state == 'INSUFFICIENT_DATA'
    assert summary.state_updated is None
    assert summary.dimensions == ()
    assert summary.actions_enabled is False
