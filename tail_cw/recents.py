"""Local memory of which log groups were opened, so active ones surface first.

Recents live in the XDG *data* directory rather than the config directory (the
user never edits them) or the cache directory (a cache sweep must not erase
them). Entries are keyed by AWS profile because log groups are account-scoped,
the same reason the profile is part of the Parquet cache key in ADR 0003.

The file is a convenience. Losing it or finding it corrupt degrades to an empty
history rather than raising, so a bad write can never block startup.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from platformdirs import user_data_dir

from tail_cw.aws.log_groups import LogGroupInfo

RECENTS_FILENAME: Final = 'recents.json'
"""Name of the recents file inside the XDG data directory."""

DEFAULT_PROFILE_KEY: Final = ''
"""Key standing in for "no ``--profile`` given"; a real profile is never empty."""

DEFAULT_RECENTS_LIMIT: Final = 20
"""How many group names are kept per profile."""


@dataclass(frozen=True)
class Recents:
    """Recently opened log group names per AWS profile, most recent first."""

    by_profile: dict[str, tuple[str, ...]] = field(default_factory=dict)


def recents_path() -> Path:
    """Return the recents file path in the XDG data directory.

    The directory is not created here; :func:`save_recents` creates it on the
    first write so merely reading history leaves no trace on disk.
    """
    return Path(user_data_dir('tail-cw')) / RECENTS_FILENAME


def profile_key(profile: str | None) -> str:
    """Return the storage key for an AWS profile name."""
    return profile or DEFAULT_PROFILE_KEY


def profile_recents(recents: Recents, profile: str | None) -> tuple[str, ...]:
    """Return the recent group names recorded under one profile."""
    return recents.by_profile.get(profile_key(profile), ())


def _clean_entry(value: Any) -> tuple[str, ...] | None:
    if not isinstance(value, list):
        return None
    return tuple(item for item in value if isinstance(item, str))


def _parse(data: Any) -> Recents:
    if not isinstance(data, dict):
        return Recents()
    by_profile = {
        key: cleaned
        for key, value in data.items()
        if isinstance(key, str) and (cleaned := _clean_entry(value)) is not None
    }
    return Recents(by_profile=by_profile)


def load_recents(path: Path | None = None) -> Recents:
    """Read the recents file, returning empty history when it is missing or unreadable.

    A malformed entry is dropped on its own; the remaining profiles survive.
    """
    target = path if path is not None else recents_path()
    try:
        raw = target.read_text(encoding='utf-8')
    except OSError:
        return Recents()
    try:
        return _parse(json.loads(raw))
    except json.JSONDecodeError:
        return Recents()


def save_recents(recents: Recents, path: Path | None = None) -> None:
    """Write the recents file atomically, creating the data directory as needed.

    An unwritable path propagates ``OSError``; callers that treat the history as
    optional catch it.
    """
    target = path if path is not None else recents_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({key: list(value) for key, value in sorted(recents.by_profile.items())})
    temp_path = target.with_suffix('.tmp')
    temp_path.write_text(payload, encoding='utf-8')
    temp_path.replace(target)


def record_selection(
    recents: Recents,
    groups: Sequence[str],
    *,
    profile: str | None,
    limit: int = DEFAULT_RECENTS_LIMIT,
) -> Recents:
    """Return new recents with ``groups`` moved to the front of one profile's history.

    Groups keep the order the user selected them in, duplicates collapse to the
    first occurrence, and the history is truncated to ``limit``. An empty
    selection leaves the history untouched.
    """
    selected = tuple(dict.fromkeys(groups))
    if not selected:
        return recents
    key = profile_key(profile)
    kept = tuple(name for name in recents.by_profile.get(key, ()) if name not in set(selected))
    return Recents(by_profile={**recents.by_profile, key: (selected + kept)[:limit]})


def sort_by_recency(groups: Sequence[LogGroupInfo], recent: Sequence[str]) -> list[LogGroupInfo]:
    """Return the groups with recently opened ones first, in recency order.

    A stable partition: groups named in ``recent`` lead in that order, and every
    other group follows in its original order. Names in ``recent`` that no longer
    exist are simply absent.
    """
    rank = {name: index for index, name in enumerate(recent)}
    leading = sorted((group for group in groups if group.name in rank), key=lambda group: rank[group.name])
    return leading + [group for group in groups if group.name not in rank]
