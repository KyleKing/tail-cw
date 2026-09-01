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

A role's color can instead be read from the active Textual theme, when a caller
passes its `theme_variables` mapping. This stays a pure function of its
arguments (no app import, no module-level app binding) so it works from call
sites that are not themselves widgets. The fixed hex above is the fallback when
no theme is given.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
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

_ROLE_VARIABLES: dict[MetricRole, str] = {
    MetricRole.ERRORS: 'error',
    MetricRole.LATENCY: 'warning',
    MetricRole.AVAILABILITY: 'success',
    MetricRole.TRAFFIC: 'accent',
    MetricRole.SATURATION: 'secondary',
}

_CONTRAST_FLOOR = 2.0
"""Ratio linecast's `_theme.py` uses for non-text chart ink, not the 4.5:1 WCAG text floor."""

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


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    text = hex_color.lstrip('#')
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)


def _rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    return '#' + ''.join(f'{max(0, min(255, channel)):02x}' for channel in rgb)


_SRGB_LINEAR_THRESHOLD = 0.03928
_MID_LUMINANCE = 0.5


def _channel_luminance(channel: int) -> float:
    value = channel / 255
    return value / 12.92 if value <= _SRGB_LINEAR_THRESHOLD else ((value + 0.055) / 1.055) ** 2.4


def _relative_luminance(hex_color: str) -> float:
    red, green, blue = _hex_to_rgb(hex_color)
    return 0.2126 * _channel_luminance(red) + 0.7152 * _channel_luminance(green) + 0.0722 * _channel_luminance(blue)


def _contrast_ratio(first: str, second: str) -> float:
    lighter, darker = sorted((_relative_luminance(first), _relative_luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def ensure_contrast(color: str, background: str, *, ratio: float = _CONTRAST_FLOOR) -> str:
    """Walk `color` toward black or white until it clears `ratio` against `background`.

    For chart ink, not body text: linecast's `_theme.py` uses the same 2.0 floor for its
    non-text drawing.
    """
    if _contrast_ratio(color, background) >= ratio:
        return color
    target = (255, 255, 255) if _relative_luminance(background) < _MID_LUMINANCE else (0, 0, 0)
    start_r, start_g, start_b = _hex_to_rgb(color)
    target_r, target_g, target_b = target
    for step in range(1, 21):
        fraction = step / 20
        blended = (
            round(start_r + (target_r - start_r) * fraction),
            round(start_g + (target_g - start_g) * fraction),
            round(start_b + (target_b - start_b) * fraction),
        )
        candidate = _rgb_to_hex(blended)
        if _contrast_ratio(candidate, background) >= ratio:
            return candidate
    return _rgb_to_hex(target)


def role_color(label: str, *, role: MetricRole | None = None, theme_colors: Mapping[str, str] | None = None) -> str:
    """Return the cohesive color for a metric.

    Uses the explicit ``role`` when given, else infers one from ``label``, and falls back
    to a color hashed from the label when no role is known. When ``theme_colors`` carries
    the active Textual theme's variables, a known role reads its color from the theme
    instead of the fixed default, walked to clear a contrast floor against the theme
    background.
    """
    resolved = role if role is not None else role_for(label)
    if resolved is None:
        return _hashed_color(label)
    default = _ROLE_COLORS[resolved]
    if theme_colors is None:
        return default
    color = theme_colors.get(_ROLE_VARIABLES[resolved], default)
    background = theme_colors.get('background')
    return ensure_contrast(color, background) if background else color


def series_color(index: int) -> str:
    """Return the categorical color for the nth series within a single chart."""
    return CATEGORICAL[index % len(CATEGORICAL)]
