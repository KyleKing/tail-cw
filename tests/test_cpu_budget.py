"""Tests for the native-engine CPU budget."""

import os

import pytest

from tail_cw import cpu_budget
from tail_cw.cpu_budget import (
    CPU_FRACTION_ENV,
    MAX_THREADS_ENV,
    POLARS_THREADS_ENV,
    apply_native_thread_limits,
    duckdb_threads,
    max_threads,
)


@pytest.fixture
def twelve_cores(monkeypatch):
    monkeypatch.setattr(cpu_budget, 'cpu_count', lambda: 12)
    monkeypatch.delenv(MAX_THREADS_ENV, raising=False)
    monkeypatch.delenv(CPU_FRACTION_ENV, raising=False)


@pytest.mark.usefixtures('twelve_cores')
def test_max_threads_defaults_to_a_minority_of_the_machine():
    assert max_threads() == 4


@pytest.mark.usefixtures('twelve_cores')
@pytest.mark.parametrize(
    ('fraction', 'expected'),
    [('0.5', 6), ('1.0', 12), ('0.01', 1), ('2.0', 12)],
)
def test_cpu_fraction_scales_the_budget(monkeypatch, fraction, expected):
    monkeypatch.setenv(CPU_FRACTION_ENV, fraction)

    assert max_threads() == expected


@pytest.mark.usefixtures('twelve_cores')
def test_max_threads_override_wins_but_cannot_exceed_the_machine(monkeypatch):
    monkeypatch.setenv(MAX_THREADS_ENV, '8')
    monkeypatch.setenv(CPU_FRACTION_ENV, '0.1')
    assert max_threads() == 8

    monkeypatch.setenv(MAX_THREADS_ENV, '999')
    assert max_threads() == 12


@pytest.mark.usefixtures('twelve_cores')
@pytest.mark.parametrize('value', ['', 'lots', '0', '-4'])
def test_an_unusable_override_falls_back_to_the_default(monkeypatch, value):
    """A typo in the environment must not fail the command."""
    monkeypatch.setenv(MAX_THREADS_ENV, value)
    monkeypatch.setenv(CPU_FRACTION_ENV, value)

    assert max_threads() == 4


@pytest.mark.usefixtures('twelve_cores')
def test_duckdb_gets_a_share_of_the_budget_because_its_pool_is_per_connection():
    assert duckdb_threads() == 1


def test_duckdb_threads_never_drops_below_one(monkeypatch):
    monkeypatch.setattr(cpu_budget, 'cpu_count', lambda: 1)
    monkeypatch.delenv(MAX_THREADS_ENV, raising=False)
    monkeypatch.delenv(CPU_FRACTION_ENV, raising=False)

    assert max_threads() == 1
    assert duckdb_threads() == 1


@pytest.mark.usefixtures('twelve_cores')
def test_apply_native_thread_limits_publishes_the_budget(monkeypatch):
    monkeypatch.delenv(POLARS_THREADS_ENV, raising=False)

    apply_native_thread_limits()

    assert os.environ[POLARS_THREADS_ENV] == '4'


@pytest.mark.usefixtures('twelve_cores')
def test_apply_native_thread_limits_leaves_an_explicit_setting_alone(monkeypatch):
    """A caller who set the Polars limit in their shell meant it."""
    monkeypatch.setenv(POLARS_THREADS_ENV, '9')

    apply_native_thread_limits()

    assert os.environ[POLARS_THREADS_ENV] == '9'
