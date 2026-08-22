"""Cap how much of the machine the native query engines may take.

DuckDB and Polars each size their own thread pool from the CPU count, and the blocking pool
runs several of their calls at once, so the default is heavy oversubscription: a multi-group
fetch was measured at 906% CPU (7.6 of 12 cores) writing Parquet. Nothing about that is
faster for the user, and it makes the machine unusable while it runs.

The budget is a share of the CPU count, applied to both engines. ``POLARS_MAX_THREADS`` is
read by Polars when it is imported and cannot be changed afterwards, so
:func:`apply_native_thread_limits` has to run before anything imports Polars, which is why
``tail_cw/__init__.py`` calls it first.
"""

from __future__ import annotations

import os

from tail_cw.concurrency import DEFAULT_BLOCKING_WORKERS

CPU_FRACTION_ENV = 'TAIL_CW_CPU_FRACTION'
MAX_THREADS_ENV = 'TAIL_CW_MAX_THREADS'
POLARS_THREADS_ENV = 'POLARS_MAX_THREADS'
DEFAULT_CPU_FRACTION = 0.4


def cpu_count() -> int:
    """Return usable CPUs, honoring a cgroup or affinity limit where the OS reports one."""
    affinity = getattr(os, 'sched_getaffinity', None)
    if affinity is not None:
        return max(1, len(affinity(0)))
    return max(1, os.cpu_count() or 1)


def max_threads() -> int:
    """Return the total native threads tail-cw may run.

    ``TAIL_CW_MAX_THREADS`` overrides the budget outright. Otherwise it is
    ``TAIL_CW_CPU_FRACTION`` (default 0.4) of the CPU count, never below one. An unparseable
    or non-positive value in either falls back to the default rather than failing a command
    over a environment typo.
    """
    override = _positive_int(os.environ.get(MAX_THREADS_ENV))
    if override is not None:
        return min(override, cpu_count())
    fraction = _positive_float(os.environ.get(CPU_FRACTION_ENV)) or DEFAULT_CPU_FRACTION
    return max(1, int(cpu_count() * min(fraction, 1.0)))


def duckdb_threads() -> int:
    """Return the per-connection thread count.

    DuckDB sizes its pool per connection and the blocking pool can hold several queries at
    once, so the budget is divided by the pool width to keep the total inside it. Polars needs
    no such division: its pool is process-wide.
    """
    return max(1, max_threads() // DEFAULT_BLOCKING_WORKERS)


def apply_native_thread_limits() -> None:
    """Publish the budget to engines that read it from the environment at import time.

    An existing ``POLARS_MAX_THREADS`` is left alone, so an explicit setting from the caller's
    shell still wins.
    """
    os.environ.setdefault(POLARS_THREADS_ENV, str(max_threads()))


def _positive_int(value: str | None) -> int | None:
    try:
        parsed = int(value) if value else 0
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def _positive_float(value: str | None) -> float | None:
    try:
        parsed = float(value) if value else 0.0
    except ValueError:
        return None
    return parsed if parsed > 0 else None
