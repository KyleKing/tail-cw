"""Tests for semantic metric colors."""

from __future__ import annotations

import pytest

from tail_cw.charts.palette import MetricRole, ensure_contrast, role_color, role_for, series_color


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


def test_role_color_reads_the_theme_variable_for_its_role() -> None:
    theme_colors = {'error': '#ff0000', 'background': '#000000'}
    assert role_color('5xx errors', theme_colors=theme_colors) == '#ff0000'


def test_role_color_falls_back_to_fixed_hex_without_a_theme() -> None:
    assert role_color('5xx errors', theme_colors=None) == role_color('5xx errors')


def test_ensure_contrast_leaves_a_color_that_already_clears_the_floor() -> None:
    assert ensure_contrast('#ffffff', '#000000') == '#ffffff'


def test_ensure_contrast_walks_a_low_contrast_color_toward_the_background_opposite() -> None:
    # Amber on a near-white background reads at 1.75:1, under the 2.0 floor
    # AGENTS.local.md records hitting by hand once at 1.9:1.
    walked = ensure_contrast('#f6ae2d', '#f5f5f5')
    assert walked != '#f6ae2d'
    r, g, b = int(walked[1:3], 16), int(walked[3:5], 16), int(walked[5:7], 16)
    assert (r, g, b) < (0xF6, 0xAE, 0x2D)
