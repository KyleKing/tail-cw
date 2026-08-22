"""Cover the OTLP document tail-cw hands to a trace viewer."""

import json
from datetime import timedelta
from typing import Any

import pytest

from tail_cw.aws.events import LogEvent
from tail_cw.query.otlp import STATUS_CODE_ERROR, trace_error_summary, trace_groups_to_otlp
from tail_cw.query.trace import TraceGroup, create_trace_groups, group_events_by_trace
from tests.factories import BASE_TIME, make_event

TRACE_ID = '1-68a1f2c3-4d5e6f708192a3b4c5d6e7f8'


def _record(**fields: Any) -> str:
    return json.dumps({'trace_id': TRACE_ID, **fields})


def _groups(*events: LogEvent) -> list[TraceGroup]:
    return create_trace_groups(group_events_by_trace(list(events)))


def _spans(document: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        span for resource in document['resourceSpans'] for scope in resource['scopeSpans'] for span in scope['spans']
    ]


def _single_span(**fields: Any) -> dict[str, Any]:
    event = make_event(_record(**fields), timestamp=BASE_TIME + timedelta(seconds=1))
    return _spans(trace_groups_to_otlp(_groups(event)))[0]


def test_ids_are_coerced_to_the_fixed_width_hex_otlp_requires():
    span = _single_span(span_id='4d5e6f708192a3b4', event='call')

    assert span['traceId'] == '68a1f2c34d5e6f708192a3b4c5d6e7f8'
    assert span['spanId'] == '4d5e6f708192a3b4'
    assert span['name'] == 'call'


def test_a_missing_span_id_is_derived_from_the_line_so_two_exports_agree():
    first = _single_span(event='call')
    second = _single_span(event='call')

    assert first['spanId'] == second['spanId']
    assert len(first['spanId']) == 16


def test_a_non_hex_identifier_is_hashed_rather_than_mangled():
    span = _single_span(span_id='worker-task-42')

    assert span['spanId'] != 'worker-task-42'
    assert len(span['spanId']) == 16
    int(span['spanId'], 16)


@pytest.mark.parametrize(
    ('fields', 'expected_ms'),
    [
        # A service logs when the work finishes, so the interval ends at the line.
        ({'duration_ms': 250.0}, 250.0),
        ({}, 0.0),
    ],
)
def test_a_span_covers_the_work_that_preceded_its_line(fields: dict[str, Any], expected_ms: float):
    span = _single_span(**fields)

    elapsed_ms = (int(span['endTimeUnixNano']) - int(span['startTimeUnixNano'])) / 1_000_000
    assert elapsed_ms == pytest.approx(expected_ms)
    assert int(span['endTimeUnixNano']) == int((BASE_TIME + timedelta(seconds=1)).timestamp() * 1_000_000_000)


def test_an_error_span_carries_the_otlp_error_status_and_its_fields():
    span = _single_span(level='error', status_code=502, event='downstream_call_failed')

    assert span['status'] == {'code': STATUS_CODE_ERROR}
    attributes = {item['key']: next(iter(item['value'].values())) for item in span['attributes']}
    assert attributes['status_code'] == '502'
    assert attributes['aws.log_group'] == '/aws/test/group'


def test_spans_are_grouped_into_one_resource_per_service():
    document = trace_groups_to_otlp(
        _groups(
            make_event(_record(service='api', event='in'), log_group='/aws/ecs/api'),
            make_event(_record(service='worker', event='out'), log_group='/aws/ecs/worker'),
        ),
    )

    services = {
        attribute['value']['stringValue']
        for resource in document['resourceSpans']
        for attribute in resource['resource']['attributes']
    }
    assert services == {'api', 'worker'}


def test_the_summary_names_the_first_service_to_fail():
    groups = _groups(
        make_event(_record(service='api', event='in'), timestamp=BASE_TIME),
        make_event(
            _record(service='payments', level='error', event='boom'), timestamp=BASE_TIME + timedelta(seconds=1)
        ),
    )

    summary = trace_error_summary(groups[0])

    assert 'first error in payments' in summary
    assert '2 spans across 2 services' in summary


def test_the_summary_says_so_when_nothing_failed():
    assert 'no errors' in trace_error_summary(_groups(make_event(_record(event='fine')))[0])


def test_lines_sharing_a_span_id_become_one_span_with_events():
    """A service logs many lines inside one span; 72 spans sharing an id is not a trace."""
    lines = [
        make_event(
            _record(span_id='4d5e6f708192a3b4', event=f'step {index}', duration_ms=index * 10),
            timestamp=BASE_TIME + timedelta(seconds=index),
        )
        for index in range(3)
    ]

    spans = _spans(trace_groups_to_otlp(_groups(*lines)))

    assert len(spans) == 1
    assert [event['name'] for event in spans[0]['events']] == ['step 0', 'step 1', 'step 2']
    assert spans[0]['name'] == 'step 2', 'the longest piece of work names the span'
    elapsed_ms = (int(spans[0]['endTimeUnixNano']) - int(spans[0]['startTimeUnixNano'])) / 1_000_000
    assert elapsed_ms == pytest.approx(2000.0), 'the span covers the earliest line to the latest'


def test_lines_with_no_span_id_stay_separate_spans():
    lines = [
        make_event(_record(event=f'step {index}'), timestamp=BASE_TIME + timedelta(seconds=index)) for index in range(3)
    ]

    spans = _spans(trace_groups_to_otlp(_groups(*lines)))

    assert len(spans) == 3
    assert all('events' not in span for span in spans)
