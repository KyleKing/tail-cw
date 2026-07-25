"""Cover the demo's traffic model and its log-field contract with tail-cw."""

import json
import random
from datetime import UTC, datetime

import pytest
import scenario

from tail_cw.aws.client import LogEvent
from tail_cw.query.trace import (
    extract_service_name,
    extract_span_metadata,
    extract_trace_id_from_event,
    is_error_event,
)

SAMPLE_COUNT = 4000


def _event(record: dict[str, object], log_group: str = '/aws/lambda/tail-cw-demo-payments') -> LogEvent:
    return LogEvent(
        log_group=log_group,
        log_stream='2026/07/25/[$LATEST]abc',
        timestamp=datetime(2026, 7, 25, 12, 0, tzinfo=UTC),
        message=json.dumps(record),
        event_id='1',
        ingestion_time=None,
    )


@pytest.mark.parametrize(
    ('elapsed_minutes', 'expected'),
    [
        (0.0, scenario.Phase.WARMUP),
        (1.99, scenario.Phase.WARMUP),
        (2.0, scenario.Phase.STEADY),
        (5.99, scenario.Phase.STEADY),
        (6.0, scenario.Phase.INCIDENT),
        (9.99, scenario.Phase.INCIDENT),
        (10.0, scenario.Phase.RECOVERY),
        (11.99, scenario.Phase.RECOVERY),
        (12.0, scenario.Phase.STEADY),
        (600.0, scenario.Phase.STEADY),
    ],
)
def test_phase_at_boundaries(elapsed_minutes: float, expected: scenario.Phase) -> None:
    assert scenario.phase_at(elapsed_minutes).phase is expected


def test_phase_at_clamps_below_zero() -> None:
    assert scenario.phase_at(-5.0).phase is scenario.Phase.WARMUP


def test_incident_singles_out_payments() -> None:
    profile = scenario.phase_at(7.0)
    assert scenario.error_rate_for(profile, scenario.PAYMENTS) > scenario.error_rate_for(profile, scenario.ORDERS)
    assert scenario.latency_multiplier_for(profile, scenario.PAYMENTS) > scenario.latency_multiplier_for(
        profile, scenario.ORDERS
    )


def test_recovery_is_milder_than_the_incident() -> None:
    incident = scenario.phase_at(7.0)
    recovery = scenario.phase_at(11.0)
    assert scenario.error_rate_for(recovery, scenario.PAYMENTS) < scenario.error_rate_for(incident, scenario.PAYMENTS)


def test_every_service_has_failures_and_success_messages() -> None:
    rng = random.Random(0)
    for service in scenario.SERVICES:
        assert scenario.pick_failure(rng, service).status_code >= 400
        assert scenario.success_message(rng, service)


def test_routes_only_reference_known_services() -> None:
    for route in scenario.ROUTES:
        assert set(route.downstream) <= set(scenario.SERVICES)
        assert scenario.GATEWAY not in route.downstream
        assert route.base_latency_ms > 0
        assert 0.0 <= route.enqueue_probability <= 1.0


def test_pick_route_is_deterministic_for_a_seed() -> None:
    assert scenario.pick_route(random.Random(7)) is scenario.pick_route(random.Random(7))


def test_pick_route_covers_every_route() -> None:
    rng = random.Random(1)
    seen = {scenario.pick_route(rng).path for _ in range(SAMPLE_COUNT)}
    assert seen == {route.path for route in scenario.ROUTES}


def test_sample_latency_scales_with_the_multiplier() -> None:
    def median_for(multiplier: float) -> float:
        rng = random.Random(3)
        samples = sorted(scenario.sample_latency_ms(rng, 100.0, multiplier) for _ in range(SAMPLE_COUNT))
        return samples[len(samples) // 2]

    assert median_for(1.0) == pytest.approx(100.0, rel=0.1)
    assert median_for(7.0) == pytest.approx(700.0, rel=0.1)


def test_sample_latency_is_always_positive() -> None:
    rng = random.Random(11)
    assert all(scenario.sample_latency_ms(rng, 4.0, 1.0) > 0 for _ in range(SAMPLE_COUNT))


def test_should_fail_tracks_the_configured_rate() -> None:
    profile = scenario.phase_at(7.0)
    rng = random.Random(5)
    failures = sum(scenario.should_fail(rng, profile, scenario.PAYMENTS) for _ in range(SAMPLE_COUNT))
    assert failures / SAMPLE_COUNT == pytest.approx(profile.degraded_error_rate, abs=0.03)


def test_span_and_trace_ids_have_the_expected_shape() -> None:
    rng = random.Random(2)
    span_id = scenario.new_span_id(rng)
    assert len(span_id) == 16
    assert int(span_id, 16) >= 0

    root, epoch, suffix = scenario.new_trace_id(rng, 0x68A1F2C3).split('-')
    assert root == '1'
    assert epoch == '68a1f2c3'
    assert len(suffix) == 24


def test_each_request_gets_its_own_trace_id() -> None:
    epoch = 1_700_000_000
    trace_ids = {
        scenario.new_trace_id(random.Random(scenario.request_seed(epoch, index)), epoch) for index in range(500)
    }
    assert len(trace_ids) == 500


def test_request_seed_is_unique_per_request() -> None:
    seeds = {scenario.request_seed(1_700_000_000, index) for index in range(1000)}
    assert len(seeds) == 1000


def test_error_log_record_is_recognised_by_tail_cw() -> None:
    record = scenario.build_log_record(
        level='ERROR',
        event='downstream_call_failed',
        service=scenario.PAYMENTS,
        trace_id='1-68a1f2c3-4d5e6f708192a3b4c5d6e7f8',
        span_id='4d5e6f708192a3b4',
        message='payment provider timed out',
        status_code=502,
        duration_ms=812.4,
        error_type='ProviderTimeout',
    )
    event = _event(record)

    assert extract_trace_id_from_event(event) == record['trace_id']
    assert extract_service_name(event) == scenario.PAYMENTS
    assert is_error_event(event)

    metadata = extract_span_metadata(event)
    assert metadata['span_id'] == record['span_id']
    assert metadata['duration_ms'] == pytest.approx(812.4)
    assert metadata['parent_span_id'] is None


def test_success_log_record_is_not_flagged_as_an_error() -> None:
    record = scenario.build_log_record(
        level='INFO',
        event='downstream_call',
        service=scenario.ORDERS,
        trace_id='1-68a1f2c3-4d5e6f708192a3b4c5d6e7f8',
        span_id='4d5e6f708192a3b4',
        message='order created',
        status_code=200,
        duration_ms=41.2,
    )
    assert not is_error_event(_event(record, '/aws/lambda/tail-cw-demo-orders'))


def test_emf_header_declares_only_known_metrics() -> None:
    header = scenario.build_emf_header('TailCwDemo', ['RequestCount', 'RequestLatencyMs'], 1_700_000_000_000)
    declaration = header['CloudWatchMetrics'][0]

    assert declaration['Namespace'] == 'TailCwDemo'
    assert declaration['Dimensions'] == [['service_name']]
    assert [metric['Name'] for metric in declaration['Metrics']] == ['RequestCount', 'RequestLatencyMs']
    assert [metric['Unit'] for metric in declaration['Metrics']] == ['Count', 'Milliseconds']


def test_emf_header_rejects_an_undeclared_metric() -> None:
    with pytest.raises(KeyError):
        scenario.build_emf_header('TailCwDemo', ['NotAMetric'], 0)
