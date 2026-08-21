"""Merge shape keys that differ only in a literal phrase.

Normalization collapses variable tokens but keeps literal words, so one recurring event
whose text names a different field or entity each time still lands on several keys. This
merges those, replacing the disagreeing runs with a wildcard.

Comparison is quadratic in the number of keys, which is affordable only because the caller
keys on the message body first and hands over tens of shapes rather than thousands. Pure: no
AWS calls, no I/O.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher

VARIABLE_PLACEHOLDER = '<*>'
DEFAULT_SIMILARITY = 0.8
DEFAULT_MAX_KEYS = 400


@dataclass(frozen=True)
class MergedCluster:
    """One merged shape and the keys that folded into it, most frequent first."""

    key: str
    members: tuple[str, ...]


def merge_similar_keys(
    ranked_keys: Sequence[str],
    *,
    similarity: float = DEFAULT_SIMILARITY,
    max_keys: int = DEFAULT_MAX_KEYS,
) -> list[MergedCluster]:
    """Fold each key into the first sufficiently similar cluster, else start a new one.

    `ranked_keys` must be ordered most frequent first, so the heaviest shape becomes the
    representative its neighbours merge into. Keys beyond `max_keys` are returned unmerged
    rather than dropped, which bounds the comparison cost without hiding data.
    """
    clusters: list[tuple[list[str], list[str]]] = []
    for key in ranked_keys[:max_keys]:
        tokens = key.split()
        for shape, members in clusters:
            if _ratio(shape, tokens) >= similarity:
                shape[:] = _merge_tokens(shape, tokens)
                members.append(key)
                break
        else:
            clusters.append((tokens, [key]))

    merged = [MergedCluster(key=' '.join(shape), members=tuple(members)) for shape, members in clusters]
    merged.extend(MergedCluster(key=key, members=(key,)) for key in ranked_keys[max_keys:])
    return merged


def _ratio(left: Sequence[str], right: Sequence[str]) -> float:
    return SequenceMatcher(None, left, right, autojunk=False).ratio()


def _merge_tokens(left: Sequence[str], right: Sequence[str]) -> list[str]:
    matcher = SequenceMatcher(None, left, right, autojunk=False)
    merged: list[str] = []
    for tag, start, end, _, _ in matcher.get_opcodes():
        if tag == 'equal':
            merged.extend(left[start:end])
        elif merged[-1:] != [VARIABLE_PLACEHOLDER]:
            merged.append(VARIABLE_PLACEHOLDER)
    return merged
