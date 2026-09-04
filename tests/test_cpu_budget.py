"""Tests for the native-engine CPU budget."""

import os

import pytest

from tail_cw import cpu_budget
from tail_cw.cpu_budget import (
    CPU_FRACTION_ENV,
    MAX_THREADS_ENV,
    NICE_INCREMENT_ENV,
    POLARS_THREADS_ENV,
    apply_native_thread_limits,
    duckdb_threads,
    lower_priority_for_batch_work,
    max_threads,
    native_write_gate,
)


def _raise_os_error(_increment: int) -> None:
    raise OSError


@pytest.fixture
def twelve_cores(monkeypatch):
    monkeypatch.setattr(cpu_budget, 'cpu_count', lambda: 12)
    monkeypatch.setattr(cpu_budget, 'current_load', lambda: 0.0)
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
    monkeypatch.setattr(cpu_budget, 'current_load', lambda: 0.0)
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


@pytest.mark.usefixtures('twelve_cores')
@pytest.mark.parametrize(
    ('load', 'expected'),
    [(1.5, 4), (15.0, 1)],
    ids=['idle_headroom_ignored', 'heavy_load_shrinks_the_budget'],
)
def test_max_threads_accounts_for_load(monkeypatch, load, expected):
    """A fetch started while something else pegs the machine must not add its full share."""
    monkeypatch.setattr(cpu_budget, 'current_load', lambda: load)

    assert max_threads() == expected


@pytest.mark.usefixtures('twelve_cores')
def test_max_threads_override_wins_even_under_load(monkeypatch):
    """An override is an explicit choice, not a default the load headroom should second-guess."""
    monkeypatch.setattr(cpu_budget, 'current_load', lambda: 15.0)
    monkeypatch.setenv(MAX_THREADS_ENV, '8')

    assert max_threads() == 8


def test_current_load_falls_back_when_unsupported(monkeypatch):
    monkeypatch.delattr(os, 'getloadavg', raising=False)

    assert cpu_budget.current_load() == pytest.approx(0.0)


@pytest.mark.usefixtures('twelve_cores')
def test_native_write_gate_is_sized_off_the_budget():
    gate = native_write_gate()

    for _ in range(4):
        assert gate.acquire(blocking=False)
    assert gate.acquire(blocking=False) is False


@pytest.mark.parametrize(
    ('increment_env', 'expected'),
    [(None, cpu_budget.DEFAULT_NICE_INCREMENT), ('3', 3)],
    ids=['default', 'override'],
)
def test_lower_priority_for_batch_work_raises_niceness_by_the_configured_increment(
    monkeypatch, increment_env, expected
):
    calls: list[int] = []
    monkeypatch.setattr(os, 'nice', calls.append, raising=False)
    if increment_env is None:
        monkeypatch.delenv(NICE_INCREMENT_ENV, raising=False)
    else:
        monkeypatch.setenv(NICE_INCREMENT_ENV, increment_env)

    lower_priority_for_batch_work()

    assert calls == [expected]


@pytest.mark.parametrize(
    'break_os_nice',
    [
        lambda mp: mp.setattr(os, 'nice', _raise_os_error, raising=False),
        lambda mp: mp.delattr(os, 'nice', raising=False),
    ],
    ids=['refused', 'unsupported'],
)
def test_lower_priority_for_batch_work_never_fails_a_batch_command(monkeypatch, break_os_nice):
    """A batch export must not fail over a scheduling nicety."""
    break_os_nice(monkeypatch)

    lower_priority_for_batch_work()
