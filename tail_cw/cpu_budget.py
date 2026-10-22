"""Cap how much of the machine the native query engines may take.

DuckDB and Polars each size their own thread pool from the CPU count, and the blocking pool
runs several of their calls at once, so the default is heavy oversubscription: a multi-group
fetch was measured at 906% CPU (7.6 of 12 cores) writing Parquet. Nothing about that is
faster for the user, and it makes the machine unusable while it runs.

The budget is a share of the CPU count, applied to both engines, further reduced when the
machine already carries load from something else (a fetch started while Docker or another
process is busy should not add its whole share on top). ``POLARS_MAX_THREADS`` is read by
Polars when it is imported and cannot be changed afterwards, so
:func:`apply_native_thread_limits` has to run before anything imports Polars, which is why
``tail_cw/__init__.py`` calls it first.

Writing a segment to Parquet does the same native JSON-encode-and-compress work a query
does, so :func:`native_write_gate` hands out a semaphore sized off the same budget. It is
separate from the fetch pool's width: a fetch stays wide because most of a segment's life is
spent waiting on the network, but the compute at the end of it competes for the same cores a
query would and gets the same cap.
"""

from __future__ import annotations

import contextlib
import math
import os
import platform
import sys
import threading

try:
    import resource
except ImportError:
    resource = None  # type: ignore[assignment]

from tail_cw.concurrency import DEFAULT_BLOCKING_WORKERS

CPU_FRACTION_ENV = 'TAIL_CW_CPU_FRACTION'
MAX_THREADS_ENV = 'TAIL_CW_MAX_THREADS'
POLARS_THREADS_ENV = 'POLARS_MAX_THREADS'
NICE_INCREMENT_ENV = 'TAIL_CW_NICE'
LOW_IMPACT_ENV = 'TAIL_CW_LOW_IMPACT'
MAX_MEMORY_ENV = 'TAIL_CW_MAX_MEMORY_MB'
DEFAULT_CPU_FRACTION = 0.4
LOW_IMPACT_CPU_FRACTION = 0.15
DEFAULT_NICE_INCREMENT = 10
"""How much batch work (``export``) lowers its own scheduling priority by, so an interactive
foreground process is favored by the OS scheduler once the machine is actually contended.
Raising niceness never fails for a process's own priority, unlike lowering it."""

HAS_RLIMIT_AS = getattr(resource, 'RLIMIT_AS', None) is not None
"""Whether a soft address-space ceiling can be set (POSIX, minus macOS quirks aside)."""


def cpu_count() -> int:
    """Return usable CPUs, honoring a cgroup or affinity limit where the OS reports one."""
    affinity = getattr(os, 'sched_getaffinity', None)
    if affinity is not None:
        return max(1, len(affinity(0)))
    return max(1, os.cpu_count() or 1)


def current_load() -> float:
    """Return the 1-minute load average, or 0.0 where the platform has none (Windows)."""
    getloadavg = getattr(os, 'getloadavg', None)
    if getloadavg is None:
        return 0.0
    try:
        return getloadavg()[0]
    except OSError:
        return 0.0


def low_impact() -> bool:
    """Whether the run was asked to stay out of the machine's way.

    ``TAIL_CW_LOW_IMPACT=1`` or a ``--low-impact`` argument; argv is scanned
    because the Polars pool is sized at import, before any parser runs.
    """
    return os.environ.get(LOW_IMPACT_ENV) == '1' or '--low-impact' in sys.argv


def max_threads() -> int:
    """Return the total native threads tail-cw may run.

    ``TAIL_CW_MAX_THREADS`` overrides the budget outright. Otherwise it is the lesser of
    ``TAIL_CW_CPU_FRACTION`` (default 0.4, or 0.15 under ``--low-impact``) of the CPU
    count and the headroom the machine currently has (CPU count minus the 1-minute load
    average), never below one. An unparseable or non-positive value in either env var
    falls back to the default rather than failing a command over a environment typo.
    """
    override = _positive_int(os.environ.get(MAX_THREADS_ENV))
    if override is not None:
        return min(override, cpu_count())
    default = LOW_IMPACT_CPU_FRACTION if low_impact() else DEFAULT_CPU_FRACTION
    fraction = _positive_float(os.environ.get(CPU_FRACTION_ENV)) or default
    budget = max(1, int(cpu_count() * min(fraction, 1.0)))
    headroom = max(1, math.floor(cpu_count() - current_load()))
    return max(1, min(budget, headroom))


def duckdb_threads() -> int:
    """Return the per-connection thread count.

    DuckDB sizes its pool per connection and the blocking pool can hold several queries at
    once, so the budget is divided by the pool width to keep the total inside it. Polars needs
    no such division: its pool is process-wide.
    """
    return max(1, max_threads() // DEFAULT_BLOCKING_WORKERS)


def native_write_gate() -> threading.Semaphore:
    """Return a fresh semaphore bounding concurrent CPU-bound Parquet writes.

    Sized off :func:`max_threads`, the same budget the query engines get. Built by the
    caller once per command (mirroring how a segment-fetch limiter is built), not cached
    here, so a long-running TUI session can pick up a lower budget on its next command
    rather than being stuck with the load reading from when it started.
    """
    return threading.Semaphore(max_threads())


def apply_native_thread_limits() -> None:
    """Publish the budget to engines that read it from the environment at import time.

    An existing ``POLARS_MAX_THREADS`` is left alone, so an explicit setting from the caller's
    shell still wins.
    """
    os.environ.setdefault(POLARS_THREADS_ENV, str(max_threads()))


def apply_memory_ceiling(max_mb: int | None = None) -> int | None:
    """Bound the process's address space.

    ``TAIL_CW_MAX_MEMORY_MB`` or ``[resources].max_memory_mb`` supplies the
    value (the argument wins); when neither does, a fraction of physical memory
    is derived, so a runaway still trips a bound instead of swapping the
    machine. A soft RLIMIT is the only portable lever over the native layers'
    allocation; past it the engines raise rather than swap. Best-effort: where
    setrlimit is absent (Windows) or refused, the command still runs — the
    refusal only warns when the ceiling was asked for explicitly, since a
    derived default the user never requested is not worth a stderr line.

    Returns:
        The effective ceiling in MB (explicit or derived), or None when no
        memory could be measured, so callers can tighten other budgets.
    """
    explicit = max_mb if max_mb and max_mb > 0 else _positive_int(os.environ.get(MAX_MEMORY_ENV))
    mb = explicit if explicit is not None else default_memory_ceiling_mb()
    _MEMORY_CEILING['mb'] = mb
    setrlimit = getattr(resource, 'setrlimit', None)
    rlimit_as = getattr(resource, 'RLIMIT_AS', None)
    getrlimit = getattr(resource, 'getrlimit', None)
    if mb is None or setrlimit is None or rlimit_as is None or getrlimit is None:
        return mb
    try:
        _soft, hard = getrlimit(rlimit_as)
        setrlimit(rlimit_as, (mb * 1024 * 1024, hard))
    except (OSError, ValueError):
        # macOS refuses RLIMIT_AS at every value, so the warning has to say the
        # ceiling was asked for and could not be set, not just that it is absent.
        if explicit is not None:
            sys.stderr.write(f'{MAX_MEMORY_ENV}={mb} could not be applied on this platform\n')
    return mb


def renice_fetch_thread() -> None:
    """Lower this fetch thread's priority under the low-impact profile.

    ``nice()`` is per-thread on Linux and per-process on macOS/BSD, so it is
    applied only where it cannot drag the interactive app down with it.
    """
    if not low_impact() or platform.system() != 'Linux':
        return
    nice = getattr(os, 'nice', None)
    if nice is not None:
        with contextlib.suppress(OSError):
            nice(DEFAULT_NICE_INCREMENT)


def lower_priority_for_batch_work() -> None:
    """Raise this process's niceness so it yields to interactive work under contention.

    Only meaningful for a one-shot batch command (``export``): a long-lived interactive
    session (the TUI) stays at normal priority because a user is actively waiting on it.
    A no-op wherever ``os.nice`` is unavailable (Windows) or refused, since a batch export
    must not fail over a scheduling nicety.
    """
    nice = getattr(os, 'nice', None)
    if nice is None:
        return
    increment = _positive_int(os.environ.get(NICE_INCREMENT_ENV))
    if increment is None:
        increment = DEFAULT_NICE_INCREMENT
    with contextlib.suppress(OSError):
        nice(increment)


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


DEFAULT_MEMORY_FRACTION = 0.5
"""Share of physical memory a run may address when no ceiling is configured."""

TIGHT_MEMORY_MB = 2048
"""A ceiling under this counts as tight: segment writers shrink to leave headroom."""

_MEMORY_CEILING: dict[str, int | None] = {'mb': None}
"""The ceiling the running process committed to, visible to the query engines."""


def physical_memory_mb() -> int | None:
    """Physical RAM in MB, or None where the OS will not say (Windows)."""
    sysconf = getattr(os, 'sysconf', None)
    if sysconf is None:
        return None
    try:
        return int(sysconf('SC_PHYS_PAGES') * sysconf('SC_PAGE_SIZE') / 1048576)
    except (OSError, ValueError):
        return None


def default_memory_ceiling_mb() -> int | None:
    """The ceiling a run gets when nothing configures one: a fraction of physical RAM."""
    physical = physical_memory_mb()
    return int(physical * DEFAULT_MEMORY_FRACTION) if physical else None


def memory_ceiling_mb() -> int | None:
    """The ceiling in force, for parts of the process that can honor it natively."""
    return _MEMORY_CEILING['mb']


def duckdb_memory_limit() -> str | None:
    """The ``SET memory_limit`` value a DuckDB connection should adopt, if any."""
    mb = _MEMORY_CEILING['mb']
    return f'{max(256, mb // 2)}MB' if mb else None
