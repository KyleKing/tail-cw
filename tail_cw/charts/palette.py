"""Semantic, cohesive colors for metrics.

A metric's role (errors, latency, traffic, saturation, availability) picks a
fixed color so the same kind of signal reads the same across every panel, which
is the thing the CloudWatch console does not do. Anything without a known role
gets a stable color hashed from its label, so a given series keeps its color
between renders. Roles are inferred from the panel title or metric name and can
be overridden in native config.

Role colors set a panel's accent and its compact-cell sparkline (cross-panel
cohesion). Multiple series inside one focused chart use the categorical palette
so they stay distinct from each other.
"""

from __future__ import annotations

import hashlib
from enum import StrEnum

_ROLE_RED = '#e5484d'
_ROLE_AMBER = '#f6ae2d'
_ROLE_BLUE = '#5aa9e6'
_ROLE_PURPLE = '#c77dff'
_ROLE_GREEN = '#8ac926'

CATEGORICAL: tuple[str, ...] = (
    '#5aa9e6',
    '#f6ae2d',
    '#f26419',
    '#8ac926',
    '#c77dff',
    '#ff5d8f',
    '#4ecdc4',
    '#ffd166',
)


class MetricRole(StrEnum):
    """Semantic role a metric plays, used to pick a cohesive color."""

    ERRORS = 'errors'
    LATENCY = 'latency'
    TRAFFIC = 'traffic'
    SATURATION = 'saturation'
    AVAILABILITY = 'availability'


_ROLE_COLORS: dict[MetricRole, str] = {
    MetricRole.ERRORS: _ROLE_RED,
    MetricRole.LATENCY: _ROLE_AMBER,
    MetricRole.TRAFFIC: _ROLE_BLUE,
    MetricRole.SATURATION: _ROLE_PURPLE,
    MetricRole.AVAILABILITY: _ROLE_GREEN,
}

_ROLE_KEYWORDS: tuple[tuple[MetricRole, tuple[str, ...]], ...] = (
    (MetricRole.ERRORS, ('error', '5xx', '4xx', 'fail', 'fault', 'exception', 'throttle')),
    (MetricRole.LATENCY, ('latency', 'duration', 'responsetime', 'response time', 'p50', 'p90', 'p95', 'p99', 'ms')),
    (MetricRole.AVAILABILITY, ('availab', 'uptime', 'healthy', 'success', 'ok')),
    (MetricRole.SATURATION, ('cpu', 'memory', 'mem', 'utilization', 'saturation', 'disk', 'queue', 'connections')),
    (MetricRole.TRAFFIC, ('request', 'count', 'traffic', 'throughput', 'invocation', 'rate', 'bytes', 'ingest')),
)


def role_for(label: str) -> MetricRole | None:
    """Infer a metric role from a title or metric name, or None if unknown."""
    text = label.lower()
    for role, keywords in _ROLE_KEYWORDS:
        if any(keyword in text for keyword in keywords):
            return role
    return None


def _hashed_color(label: str) -> str:
    digest = hashlib.blake2b(label.encode('utf-8'), digest_size=2).digest()
    return CATEGORICAL[int.from_bytes(digest, 'big') % len(CATEGORICAL)]


def role_color(label: str, *, role: MetricRole | None = None) -> str:
    """Return the cohesive color for a metric.

    Uses the explicit ``role`` when given, else infers one from ``label``, and
    falls back to a color hashed from the label when no role is known.
    """
    resolved = role if role is not None else role_for(label)
    return _ROLE_COLORS[resolved] if resolved is not None else _hashed_color(label)


def series_color(index: int) -> str:
    """Return the categorical color for the nth series within a single chart."""
    return CATEGORICAL[index % len(CATEGORICAL)]
