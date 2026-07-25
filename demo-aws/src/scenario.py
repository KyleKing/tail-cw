"""Deterministic traffic, latency, and failure model shared by every demo Lambda.

Pure stdlib and free of side effects, so the whole shape of the demo can be unit tested
without AWS. All I/O lives in `handler`.

Every seeded call takes an explicit `random.Random`, so a given (run, request) pair
replays identically across services and across `tofu apply` cycles.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

GATEWAY = 'gateway'
ORDERS = 'orders'
PAYMENTS = 'payments'
INVENTORY = 'inventory'
WORKER = 'worker'

SERVICES = (GATEWAY, ORDERS, PAYMENTS, INVENTORY, WORKER)

RAISE_PROBABILITY = 0.3
"""Share of failures that raise instead of returning a 5xx, so AWS/Lambda Errors is non-zero."""

POISON_PROBABILITY = 0.03
"""Share of queued tasks that can never succeed, so the dead-letter queue fills during a run."""

CLIENT_ERROR_STATUS = 400
SERVER_ERROR_STATUS = 500
BAD_GATEWAY_STATUS = 502

LATENCY_SIGMA = 0.45
"""Log-normal spread that gives p99 a visible tail above p50 without absurd outliers."""


class Phase(StrEnum):
    """Named stage of the run, driving error rate and latency."""

    WARMUP = 'warmup'
    STEADY = 'steady'
    INCIDENT = 'incident'
    RECOVERY = 'recovery'


@dataclass(frozen=True)
class PhaseProfile:
    """Error and latency behaviour for one stage of the run.

    A single service can be singled out as `degraded` so the incident looks like one
    dependency failing rather than uniform noise everywhere.
    """

    phase: Phase
    error_rate: float
    latency_multiplier: float
    degraded_service: str | None = None
    degraded_error_rate: float = 0.0
    degraded_latency_multiplier: float = 1.0


@dataclass(frozen=True)
class Route:
    """One HTTP-ish entry point the gateway serves."""

    method: str
    path: str
    downstream: tuple[str, ...]
    base_latency_ms: float
    weight: float
    enqueue_probability: float = 0.0


@dataclass(frozen=True)
class Failure:
    """A named way a service can fail, carrying the status and message it logs."""

    status_code: int
    error_type: str
    message: str


_SCHEDULE: tuple[tuple[float, PhaseProfile], ...] = (
    (0.0, PhaseProfile(Phase.WARMUP, error_rate=0.010, latency_multiplier=1.4)),
    (2.0, PhaseProfile(Phase.STEADY, error_rate=0.015, latency_multiplier=1.0)),
    (
        6.0,
        PhaseProfile(
            Phase.INCIDENT,
            error_rate=0.030,
            latency_multiplier=1.1,
            degraded_service=PAYMENTS,
            degraded_error_rate=0.350,
            degraded_latency_multiplier=7.0,
        ),
    ),
    (
        10.0,
        PhaseProfile(
            Phase.RECOVERY,
            error_rate=0.020,
            latency_multiplier=1.3,
            degraded_service=PAYMENTS,
            degraded_error_rate=0.100,
            degraded_latency_multiplier=2.5,
        ),
    ),
    (12.0, PhaseProfile(Phase.STEADY, error_rate=0.015, latency_multiplier=1.0)),
)

ROUTES: tuple[Route, ...] = (
    Route('GET', '/healthz', (), base_latency_ms=4.0, weight=1.0),
    Route('GET', '/v1/inventory', (INVENTORY,), base_latency_ms=28.0, weight=2.0),
    Route('GET', '/v1/orders', (ORDERS,), base_latency_ms=45.0, weight=3.0),
    Route('GET', '/v1/orders/{order_id}', (ORDERS,), base_latency_ms=34.0, weight=3.0),
    Route('POST', '/v1/payments', (PAYMENTS,), base_latency_ms=120.0, weight=2.0, enqueue_probability=0.3),
    Route(
        'POST',
        '/v1/orders',
        (ORDERS, INVENTORY, PAYMENTS),
        base_latency_ms=180.0,
        weight=4.0,
        enqueue_probability=1.0,
    ),
)

_FAILURES: dict[str, tuple[Failure, ...]] = {
    GATEWAY: (
        Failure(504, 'UpstreamTimeout', 'upstream call timed out'),
        Failure(429, 'RateLimited', 'rate limit exceeded for tenant'),
    ),
    ORDERS: (
        Failure(422, 'ValidationRejected', 'order validation rejected'),
        Failure(503, 'InventoryUnavailable', 'inventory service unavailable'),
        Failure(500, 'OrderPersistFailed', 'failed to persist order'),
    ),
    PAYMENTS: (
        Failure(502, 'ProviderTimeout', 'payment provider timed out'),
        Failure(402, 'CardDeclined', 'payment declined by issuer'),
        Failure(503, 'ProviderUnavailable', 'payment provider unavailable'),
    ),
    INVENTORY: (
        Failure(503, 'StockLookupFailed', 'stock lookup failed'),
        Failure(409, 'WarehouseSyncLag', 'warehouse sync lag exceeded threshold'),
    ),
    WORKER: (
        Failure(500, 'TaskHandlerFailed', 'task handler raised'),
        Failure(422, 'PoisonMessage', 'task payload could not be decoded'),
    ),
}

_SUCCESS_MESSAGES: dict[str, tuple[str, ...]] = {
    GATEWAY: ('request completed',),
    ORDERS: ('order retrieved', 'order created', 'order list served from cache'),
    PAYMENTS: ('payment authorized', 'payment captured'),
    INVENTORY: ('stock reserved', 'stock level read'),
    WORKER: ('task processed', 'task processed after retry'),
}


def phase_at(elapsed_minutes: float) -> PhaseProfile:
    """Return the profile in force this many minutes into the run."""
    selected = _SCHEDULE[0][1]
    for start, profile in _SCHEDULE:
        if elapsed_minutes >= start:
            selected = profile
    return selected


def error_rate_for(profile: PhaseProfile, service: str) -> float:
    """Return the failure probability for one service under this profile."""
    if service == profile.degraded_service:
        return profile.degraded_error_rate
    return profile.error_rate


def latency_multiplier_for(profile: PhaseProfile, service: str) -> float:
    """Return the latency scaling for one service under this profile."""
    if service == profile.degraded_service:
        return profile.degraded_latency_multiplier
    return profile.latency_multiplier


def pick_route(rng: random.Random) -> Route:
    """Choose a route weighted by how often a real API would see it."""
    return rng.choices(ROUTES, weights=[route.weight for route in ROUTES], k=1)[0]


def sample_latency_ms(rng: random.Random, base_ms: float, multiplier: float) -> float:
    """Draw a log-normal latency around `base_ms`, rounded to microsecond precision."""
    return round(base_ms * multiplier * math.exp(rng.normalvariate(0.0, LATENCY_SIGMA)), 3)


def should_fail(rng: random.Random, profile: PhaseProfile, service: str) -> bool:
    """Decide whether this service call fails."""
    return rng.random() < error_rate_for(profile, service)


def should_raise(rng: random.Random) -> bool:
    """Decide whether a failure surfaces as an unhandled exception rather than a 5xx body."""
    return rng.random() < RAISE_PROBABILITY


def is_poison(rng: random.Random) -> bool:
    """Decide whether a queued task is one that will never succeed."""
    return rng.random() < POISON_PROBABILITY


def resolve_gateway_status(rng: random.Random, profile: PhaseProfile, downstream_status: int) -> int:
    """Fold the worst downstream status and the gateway's own failure roll into one response code.

    A downstream 5xx becomes a 502, because the caller sees a bad gateway rather than whatever
    the inner service reported.
    """
    if should_fail(rng, profile, GATEWAY):
        return pick_failure(rng, GATEWAY).status_code
    if downstream_status >= SERVER_ERROR_STATUS:
        return BAD_GATEWAY_STATUS
    return downstream_status


def level_for_status(status_code: int) -> str:
    """Map an HTTP status onto the log level tail-cw's error detection reads."""
    if status_code >= SERVER_ERROR_STATUS:
        return 'ERROR'
    if status_code >= CLIENT_ERROR_STATUS:
        return 'WARNING'
    return 'INFO'


def pick_failure(rng: random.Random, service: str) -> Failure:
    """Choose one of the ways this service is known to fail."""
    return rng.choice(_FAILURES[service])


def success_message(rng: random.Random, service: str) -> str:
    """Choose a success message, varying the shape so the group preview clusters usefully."""
    return rng.choice(_SUCCESS_MESSAGES[service])


def new_span_id(rng: random.Random) -> str:
    """Mint a 16-hex-character span id in the W3C shape."""
    return f'{rng.getrandbits(64):016x}'


def new_trace_id(rng: random.Random, epoch_seconds: int) -> str:
    """Mint an X-Ray-shaped trace id, used only when the runtime supplies no trace header."""
    return f'1-{epoch_seconds:08x}-{rng.getrandbits(96):024x}'


def request_seed(run_epoch: int, request_index: int) -> int:
    """Derive the seed shared by every service handling one simulated request."""
    return run_epoch * 1_000_003 + request_index


METRIC_UNITS: dict[str, str] = {
    'RequestCount': 'Count',
    'RequestLatencyMs': 'Milliseconds',
    'OrdersPlaced': 'Count',
    'OrderValueUsd': 'None',
    'PaymentDeclines': 'Count',
}


def build_log_record(
    *,
    level: str,
    event: str,
    service: str,
    trace_id: str,
    span_id: str,
    message: str,
    **fields: Any,
) -> dict[str, Any]:
    """Assemble one log line.

    The key names here are a contract with tail-cw's trace and error detection: `level`,
    `service_name`, `trace_id`, `span_id`, `duration_ms`, and `status_code` are the names it
    searches for. `parent_span_id` is deliberately absent, matching ADR 0012's finding that
    these logs carry no parent links.
    """
    return {
        'level': level,
        'event': event,
        'service_name': service,
        'trace_id': trace_id,
        'span_id': span_id,
        'message': message,
        **fields,
    }


def build_emf_header(namespace: str, metric_names: list[str], timestamp_ms: int) -> dict[str, Any]:
    """Build the `_aws` header declaring which fields on a log line are metrics.

    Only `service_name` is a dimension. Route and status stay plain properties, which keeps
    the custom-metric count inside the CloudWatch free tier while leaving both searchable.
    """
    return {
        'Timestamp': timestamp_ms,
        'CloudWatchMetrics': [
            {
                'Namespace': namespace,
                'Dimensions': [['service_name']],
                'Metrics': [{'Name': name, 'Unit': METRIC_UNITS[name]} for name in metric_names],
            }
        ],
    }
