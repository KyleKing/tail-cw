"""One history of the aggregation questions asked, shared by the CLI and the TUI.

A rollup, an alarm read, or an Insights query is work worth keeping: the query
text is easy to lose and an Insights run costs money to repeat. Entries live in
the XDG *data* directory next to the group recents, for the same reasons ADR 0003
gives: the user never edits them, and a cache sweep must not erase them.

The file is a convenience. Losing it or finding it corrupt degrades to an empty
history rather than raising, so a bad write can never block a command.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from platformdirs import user_data_dir

HISTORY_FILENAME: Final = 'history.json'

DEFAULT_HISTORY_LIMIT: Final = 50
"""How many entries are kept, newest first."""

MAX_DETAIL_CHARS: Final = 4000
"""Cap on a stored result, so one wide table cannot dominate the file."""


class HistoryKind(StrEnum):
    """Which surface produced an entry."""

    ALARMS = 'alarms'
    FILTER = 'filter'
    INSIGHTS = 'insights'
    SUMMARY = 'summary'


@dataclass(frozen=True)
class HistoryEntry:
    """One recorded question and its answer.

    Attributes:
        kind: Which surface ran.
        recorded: When it ran, as an ISO timestamp. Passed in rather than
            clocked here, so recording stays a pure function.
        title: One line naming what ran: the query text, or the groups rolled up.
        window: Human-readable time range the question covered.
        detail: The rendered result, truncated to :data:`MAX_DETAIL_CHARS`.
        profile: AWS profile the question ran under, empty when unset.
    """

    kind: HistoryKind
    recorded: str
    title: str
    window: str
    detail: str
    profile: str = ''


def history_path() -> Path:
    """Return the history file path in the XDG data directory.

    The directory is not created here; :func:`save_history` creates it on the
    first write so merely reading history leaves no trace on disk.
    """
    return Path(user_data_dir('tail-cw')) / HISTORY_FILENAME


def make_entry(
    kind: HistoryKind,
    *,
    recorded: datetime,
    title: str,
    window: str,
    detail: str,
    profile: str | None = None,
) -> HistoryEntry:
    """Build an entry, truncating the detail to the stored cap."""
    body = detail if len(detail) <= MAX_DETAIL_CHARS else f'{detail[:MAX_DETAIL_CHARS]}\n… truncated'
    return HistoryEntry(
        kind=kind,
        recorded=recorded.isoformat(),
        title=title,
        window=window,
        detail=body,
        profile=profile or '',
    )


def _parse_entry(value: Any) -> HistoryEntry | None:
    if not isinstance(value, dict):
        return None
    try:
        kind = HistoryKind(value['kind'])
    except (KeyError, ValueError):
        return None
    fields = {name: value.get(name, '') for name in ('recorded', 'title', 'window', 'detail', 'profile')}
    if any(not isinstance(field, str) for field in fields.values()):
        return None
    return HistoryEntry(kind=kind, **fields)


def load_history(path: Path | None = None) -> tuple[HistoryEntry, ...]:
    """Read the history file, newest first, dropping entries that no longer parse."""
    target = path if path is not None else history_path()
    try:
        raw = target.read_text(encoding='utf-8')
    except OSError:
        return ()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return ()
    if not isinstance(data, list):
        return ()
    return tuple(entry for item in data if (entry := _parse_entry(item)) is not None)


def save_history(entries: Sequence[HistoryEntry], path: Path | None = None) -> None:
    """Write the history file atomically, creating the data directory as needed.

    An unwritable path propagates ``OSError``; callers that treat the history as
    optional catch it.
    """
    target = path if path is not None else history_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps([asdict(entry) for entry in entries])
    temp_path = target.with_suffix('.tmp')
    temp_path.write_text(payload, encoding='utf-8')
    temp_path.replace(target)


def record(
    entries: Sequence[HistoryEntry],
    entry: HistoryEntry,
    *,
    limit: int = DEFAULT_HISTORY_LIMIT,
) -> tuple[HistoryEntry, ...]:
    """Return the history with ``entry`` at the front, truncated to ``limit``."""
    return (entry, *entries)[:limit]


def append(entry: HistoryEntry, path: Path | None = None) -> None:
    """Record one entry, ignoring a history that cannot be written.

    History is never the point of the command that produced it, so a read-only
    data directory must not fail the command.
    """
    try:
        save_history(record(load_history(path), entry), path)
    except OSError:
        return
