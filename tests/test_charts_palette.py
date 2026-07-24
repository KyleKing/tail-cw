"""Tests for semantic metric colors."""

from __future__ import annotations

import pytest

from tail_cw.charts.palette import MetricRole, role_color, role_for, series_color


@pytest.mark.parametrize(
    ('label', 'role'),
    [
        ('5xx error rate (%)', MetricRole.ERRORS),
        ('Latency p99', MetricRole.LATENCY),
        ('Requests / 5 min', MetricRole.TRAFFIC),
        ('CPUUtilization', MetricRole.SATURATION),
        ('Availability %', MetricRole.AVAILABILITY),
    ],
)
def test_role_for_infers_known_roles(label: str, role: MetricRole) -> None:
    assert role_for(label) == role


def test_role_for_unknown_is_none() -> None:
    assert role_for('MysteryGauge') is None


def test_role_color_is_fixed_per_role() -> None:
    assert role_color('5xx errors') == role_color('error count')
    assert role_color('latency p50') != role_color('5xx errors')


def test_role_color_hash_fallback_is_stable() -> None:
    assert role_color('MysteryGauge') == role_color('MysteryGauge')


def test_series_color_cycles() -> None:
    assert series_color(0) == series_color(8)
    assert series_color(0) != series_color(1)
