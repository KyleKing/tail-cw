"""Schedule-driven cache warming: skip checks and high-water marks.

``tail-cw warm`` is a one-shot command a schedule runs (launchd on macOS, cron
elsewhere) rather than a resident daemon, so everything here is state a single
run can carry: a marks file per cache, an advisory lock saying a command is in
flight, and a power check the run consults before it spends anything.
"""

from __future__ import annotations

import contextlib
import json
import platform
import subprocess  # noqa: S404 - pmset is the platform's own power status tool
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

try:
    import fcntl
except ImportError:
    fcntl = None  # type: ignore[assignment]

MARKS_FILENAME = 'warm-marks.json'
"""Per-group high-water marks, as ``{log group: ISO end time}``."""

LOCK_FILENAME = 'command.lock'
"""File every cache command holds an advisory lock on while it resolves."""

HAS_FLOCK = getattr(fcntl, 'flock', None) is not None
"""Whether this platform can hold an advisory file lock (POSIX only)."""


def load_marks(cache_dir: Path) -> dict[str, datetime]:
    """Read the last end time warmed per group; a corrupt file starts cold."""
    try:
        raw = json.loads((cache_dir / MARKS_FILENAME).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    marks = {}
    for group, stamp in raw.items():
        if isinstance(stamp, str):
            with contextlib.suppress(ValueError):
                marks[group] = datetime.fromisoformat(stamp)
    return marks


def save_marks(cache_dir: Path, marks: dict[str, datetime]) -> None:
    """Write the marks atomically enough for a cron job (truncate, not crash)."""
    payload = {group: stamp.isoformat() for group, stamp in marks.items()}
    (cache_dir / MARKS_FILENAME).write_text(json.dumps(payload, indent=2), encoding='utf-8')


@contextlib.contextmanager
def command_lock(cache_dir: Path) -> Iterator[bool]:
    """Hold the advisory "a command is running" lock for this cache.

    A regular resolve does not need to win it (two exports may run at once); it
    only needs to hold it so ``warm`` can see the machine is busy. Where fcntl
    is absent (Windows) the lock always reads as held, so warm never skips for
    a phantom command and never blocks one either.

    Yields:
        True while the lock is held, False when another command has it.
    """
    flock = getattr(fcntl, 'flock', None)
    if flock is None:
        yield True
        return
    lock_ex_nb = getattr(fcntl, 'LOCK_EX', 2) | getattr(fcntl, 'LOCK_NB', 4)
    lock_un = getattr(fcntl, 'LOCK_UN', 8)
    cache_dir.mkdir(parents=True, exist_ok=True)
    handle = (cache_dir / LOCK_FILENAME).open('w')
    try:
        try:
            flock(handle.fileno(), lock_ex_nb)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            flock(handle.fileno(), lock_un)
    finally:
        handle.close()


def on_battery() -> bool:
    """Whether the machine is running off its battery right now.

    A machine with no battery, or one the platform will not describe, reads as
    mains-powered: warming is opt-in work and "unknown" should mean "go".
    """
    if platform.system() == 'Darwin':
        return _on_battery_darwin()
    return _on_battery_linux()


def _on_battery_darwin() -> bool:
    try:
        output = subprocess.run(
            ['/usr/bin/pmset', '-g', 'batt'],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    for line in output.splitlines():
        if 'drawing from' in line.lower():
            return 'battery' in line.lower()
    return False


def _on_battery_linux() -> bool:
    sysfs = Path('/sys/class/power_supply')
    if not sysfs.is_dir():
        return False
    for supply in sysfs.iterdir():
        try:
            if (supply / 'type').read_text().strip() != 'Battery':
                continue
            if (supply / 'status').read_text().strip() == 'Discharging':
                return True
        except OSError:
            continue
    return False
