"""Lambda entry point for every demo service.

One deployment package serves all five services; `SERVICE_NAME` selects the behaviour.
The gateway paces synthetic requests against its own remaining time, fans out to the
downstream services over `lambda:Invoke`, and enqueues async work on SQS. Downstream
services sleep for their sampled latency so AWS/Lambda Duration reflects the scenario.

Log lines are single-line JSON on stdout using the field names tail-cw parses:
`level`, `service_name`, `trace_id`, `span_id`, `status_code`, `duration_ms`, `message`.
`trace_id` covers one simulated request across every service that touched it, while
`xray_trace_id` names the invocation's X-Ray root so a log line can be pivoted into the
X-Ray console. The request-completion line doubles as an Embedded Metric Format record,
so the same line a human reads in the log view also produces the dashboard's custom metrics.
"""

from __future__ import annotations

import json
import os
import random
import sys
import time
from typing import Any

import boto3
import scenario

MAX_SLEEP_SECONDS = 3.0
"""Ceiling on simulated work, so an incident-phase tail cannot approach the Lambda timeout."""

GATEWAY_RESERVE_MS = 8_000
"""Remaining-time floor at which the gateway stops issuing requests and returns cleanly."""

SERVICE_NAME = os.environ['SERVICE_NAME']
RUN_EPOCH = int(os.environ['RUN_EPOCH'])
FUNCTION_PREFIX = os.environ.get('FUNCTION_PREFIX', '')
QUEUE_URL = os.environ.get('QUEUE_URL', '')
EMF_NAMESPACE = os.environ.get('EMF_NAMESPACE', 'TailCwDemo')
REQUESTS_PER_MINUTE = int(os.environ.get('REQUESTS_PER_MINUTE', '90'))

_CLIENTS: dict[str, Any] = {}


def _client(service: str) -> Any:
    if service not in _CLIENTS:
        _CLIENTS[service] = boto3.client(service)
    return _CLIENTS[service]


def _elapsed_minutes() -> float:
    return (time.time() - RUN_EPOCH) / 60.0


def _xray_trace_id() -> str | None:
    """Return the X-Ray root id of the current invocation, for pivoting from a log line to X-Ray.

    This is deliberately not the `trace_id` field. X-Ray scopes a root to one invocation, so
    the gateway's whole minute of requests shares a single root; `trace_id` is minted per
    simulated request instead, which is what makes tail-cw's trace grouping meaningful.
    """
    header = os.environ.get('_X_AMZN_TRACE_ID', '')
    for part in header.split(';'):
        if part.startswith('Root='):
            return part[len('Root=') :]
    return None


def _emit(record: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(record, separators=(',', ':')) + '\n')
    sys.stdout.flush()


def _log(
    *,
    level: str,
    event: str,
    message: str,
    trace_id: str,
    span_id: str,
    **fields: Any,
) -> dict[str, Any]:
    record = scenario.build_log_record(
        level=level,
        event=event,
        service=SERVICE_NAME,
        trace_id=trace_id,
        span_id=span_id,
        message=message,
        xray_trace_id=_xray_trace_id(),
        **fields,
    )
    _emit(record)
    return record


def _invoke_downstream(service: str, payload: dict[str, Any]) -> dict[str, Any]:
    response = _client('lambda').invoke(
        FunctionName=f'{FUNCTION_PREFIX}{service}',
        InvocationType='RequestResponse',
        Payload=json.dumps(payload).encode(),
    )
    body = json.loads(response['Payload'].read() or b'{}')
    if response.get('FunctionError'):
        return {
            'status_code': scenario.SERVER_ERROR_STATUS,
            'error_type': 'UnhandledDownstreamError',
            'service_name': service,
        }
    return body


def _handle_downstream(event: dict[str, Any]) -> dict[str, Any]:
    """Run one downstream service call: sleep for the sampled latency, then succeed or fail."""
    trace_id = event['trace_id']
    rng = random.Random(event['seed'] + scenario.SERVICES.index(SERVICE_NAME))
    span_id = scenario.new_span_id(rng)
    profile = scenario.phase_at(_elapsed_minutes())

    latency_ms = scenario.sample_latency_ms(
        rng,
        base_ms=event['base_latency_ms'],
        multiplier=scenario.latency_multiplier_for(profile, SERVICE_NAME),
    )
    time.sleep(min(latency_ms / 1000.0, MAX_SLEEP_SECONDS))

    common = {
        'trace_id': trace_id,
        'span_id': span_id,
        'duration_ms': latency_ms,
        'route': event['path'],
        'phase': profile.phase.value,
        'caller': event['caller'],
    }

    if not scenario.should_fail(rng, profile, SERVICE_NAME):
        _log(
            level='INFO',
            event='downstream_call',
            message=scenario.success_message(rng, SERVICE_NAME),
            status_code=200,
            **common,
        )
        return {'status_code': 200, 'duration_ms': latency_ms, 'service_name': SERVICE_NAME}

    failure = scenario.pick_failure(rng, SERVICE_NAME)
    _log(
        level='ERROR',
        event='downstream_call_failed',
        message=failure.message,
        status_code=failure.status_code,
        error_type=failure.error_type,
        error_message=failure.message,
        **common,
    )
    if scenario.should_raise(rng):
        raise RuntimeError(_failure_text(failure))
    return {
        'status_code': failure.status_code,
        'duration_ms': latency_ms,
        'service_name': SERVICE_NAME,
        'error_type': failure.error_type,
    }


def _failure_text(failure: scenario.Failure) -> str:
    return f'{failure.error_type}: {failure.message}'


def _enqueue(rng: random.Random, trace_id: str, seed: int, route: scenario.Route) -> None:
    body = {
        'trace_id': trace_id,
        'seed': seed,
        'route': route.path,
        'poison': scenario.is_poison(rng),
        'order_id': f'ord-{rng.getrandbits(24):06x}',
    }
    _client('sqs').send_message(QueueUrl=QUEUE_URL, MessageBody=json.dumps(body))


def _call_downstream_chain(trace_id: str, seed: int, route: scenario.Route) -> tuple[int, int]:
    """Invoke every downstream service for a route, returning the worst status and decline count."""
    worst_status = 200
    declines = 0
    per_call_latency = route.base_latency_ms / max(len(route.downstream), 1)
    for service in route.downstream:
        result = _invoke_downstream(
            service,
            {
                'trace_id': trace_id,
                'seed': seed,
                'caller': scenario.GATEWAY,
                'path': route.path,
                'base_latency_ms': per_call_latency,
            },
        )
        status = int(result.get('status_code', scenario.SERVER_ERROR_STATUS))
        declines += int(service == scenario.PAYMENTS and status >= scenario.CLIENT_ERROR_STATUS)
        worst_status = max(worst_status, status)
    return worst_status, declines


def _serve_request(request_index: int) -> None:
    """Simulate one end-to-end API request across the gateway and its downstream services."""
    seed = scenario.request_seed(RUN_EPOCH, request_index)
    rng = random.Random(seed)
    trace_id = scenario.new_trace_id(rng, int(time.time()))
    span_id = scenario.new_span_id(rng)
    route = scenario.pick_route(rng)
    profile = scenario.phase_at(_elapsed_minutes())
    started = time.monotonic()

    _log(
        level='INFO',
        event='http_request_received',
        message='request received',
        trace_id=trace_id,
        span_id=span_id,
        method=route.method,
        route=route.path,
        phase=profile.phase.value,
        customer_id=f'cust-{rng.getrandbits(16):04x}',
    )

    downstream_status, declines = _call_downstream_chain(trace_id, seed, route)
    status_code = scenario.resolve_gateway_status(rng, profile, downstream_status)
    succeeded = status_code < scenario.CLIENT_ERROR_STATUS

    if succeeded and rng.random() < route.enqueue_probability:
        _enqueue(rng, trace_id, seed, route)

    duration_ms = round((time.monotonic() - started) * 1000.0, 3)
    placed = int(succeeded and route.path == '/v1/orders' and route.method == 'POST')
    metrics: dict[str, float] = {
        'RequestCount': 1,
        'RequestLatencyMs': duration_ms,
        'OrdersPlaced': placed,
        'OrderValueUsd': round(rng.uniform(12.0, 480.0), 2) if placed else 0.0,
        'PaymentDeclines': declines,
    }
    _emit(
        scenario.build_log_record(
            level=scenario.level_for_status(status_code),
            event='http_request',
            service=SERVICE_NAME,
            trace_id=trace_id,
            span_id=span_id,
            message='request completed' if succeeded else 'request failed',
            method=route.method,
            route=route.path,
            status_code=status_code,
            duration_ms=duration_ms,
            phase=profile.phase.value,
            xray_trace_id=_xray_trace_id(),
            _aws=scenario.build_emf_header(EMF_NAMESPACE, list(metrics), int(time.time() * 1000)),
            **metrics,
        )
    )


def _handle_gateway(context: Any) -> dict[str, Any]:
    """Issue paced requests until the invocation is nearly out of time."""
    minute = int((time.time() - RUN_EPOCH) // 60)
    interval = 60.0 / max(REQUESTS_PER_MINUTE, 1)
    served = 0
    while context.get_remaining_time_in_millis() > GATEWAY_RESERVE_MS and served < REQUESTS_PER_MINUTE:
        cycle_started = time.monotonic()
        try:
            _serve_request(minute * 10_000 + served)
        except Exception as err:
            _log(
                level='ERROR',
                event='request_loop_failed',
                message='request loop raised',
                trace_id=scenario.new_trace_id(random.Random(served), int(time.time())),
                span_id='0' * 16,
                error_type=type(err).__name__,
                error_message=str(err),
            )
        served += 1
        remaining = interval - (time.monotonic() - cycle_started)
        if remaining > 0:
            time.sleep(remaining)
    return {'served': served, 'minute': minute}


def _handle_worker(event: dict[str, Any]) -> dict[str, Any]:
    """Process one SQS batch, raising so the queue retries and poison messages reach the DLQ."""
    profile = scenario.phase_at(_elapsed_minutes())
    processed = 0
    for record in event.get('Records', []):
        body = json.loads(record['body'])
        rng = random.Random(body['seed'] + scenario.SERVICES.index(scenario.WORKER))
        span_id = scenario.new_span_id(rng)
        receive_count = int(record.get('attributes', {}).get('ApproximateReceiveCount', '1'))
        latency_ms = scenario.sample_latency_ms(
            rng, base_ms=90.0, multiplier=scenario.latency_multiplier_for(profile, scenario.WORKER)
        )
        time.sleep(min(latency_ms / 1000.0, MAX_SLEEP_SECONDS))

        common = {
            'trace_id': body['trace_id'],
            'span_id': span_id,
            'duration_ms': latency_ms,
            'order_id': body['order_id'],
            'receive_count': receive_count,
            'phase': profile.phase.value,
        }
        if body['poison'] or scenario.should_fail(rng, profile, scenario.WORKER):
            failure = scenario.pick_failure(rng, scenario.WORKER)
            _log(
                level='ERROR',
                event='task_failed',
                message=failure.message,
                status_code=failure.status_code,
                error_type=failure.error_type,
                error_message=failure.message,
                **common,
            )
            raise RuntimeError(_failure_text(failure))
        _log(
            level='INFO',
            event='task_processed',
            message=scenario.success_message(rng, scenario.WORKER),
            status_code=200,
            **common,
        )
        processed += 1
    return {'processed': processed}


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Dispatch to the behaviour selected by `SERVICE_NAME`."""
    if SERVICE_NAME == scenario.GATEWAY:
        return _handle_gateway(context)
    if SERVICE_NAME == scenario.WORKER:
        return _handle_worker(event)
    return _handle_downstream(event)
