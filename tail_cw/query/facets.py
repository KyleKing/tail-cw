"""Count cached events by payload field, over the Parquet the fetch already wrote.

Answers "what is even in these logs" without knowing the payload schema, which
is the question a caller otherwise answers with ``jq`` and a ``Counter``. The
counting runs in DuckDB against the cache, so it costs no AWS call and no
transfer, and one set of functions serves both ``export stats`` and the log
view's field panel.

Blocking work: call these through :mod:`tail_cw.concurrency`, never on the
message loop.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb
import polars as pl

from tail_cw.concurrency import is_engine_panic
from tail_cw.cpu_budget import duckdb_threads
from tail_cw.query.engine import EnginePanicError, build_field_reference, duckdb_source_and_where
from tail_cw.query.parser import FilterNode

MAX_FIELD_DEPTH = 4
"""How far into a payload a discovered field path may go.

A deeply nested object is a structure rather than a facet, and enumerating every
leaf of one turns the panel into a schema dump.
"""

NULL_LABEL = '(absent)'
"""Stands in for a record that does not carry the field, which is itself a fact worth counting."""


@dataclass(frozen=True)
class FacetValue:
    """One value of one field, and how many records carried it."""

    value: str
    count: int


@dataclass(frozen=True)
class FieldFacet:
    """What one payload field holds across the events read.

    Attributes:
        path: Dotted field path, without the ``parsed`` prefix.
        values: Most common values first, capped by the caller's ``top``.
        present: Records carrying the field at all.
        distinct: Distinct values, which may exceed ``len(values)``.
    """

    path: str
    values: tuple[FacetValue, ...]
    present: int
    distinct: int

    @property
    def truncated(self) -> bool:
        """True when the field holds values this facet does not name."""
        return self.distinct > len(self.values)


IDENTIFIER_MIN_RECORDS = 5
"""Records a field needs before its cardinality says anything about it."""


def is_identifier_like(facet: FieldFacet) -> bool:
    """True when nearly every record carries its own value, which makes a poor facet.

    A trace id counted this way lists one value per record with a count of one
    each, crowding out the field that actually groups.
    """
    return facet.present >= IDENTIFIER_MIN_RECORDS and facet.distinct >= facet.present


def worth_showing(facets: Sequence[FieldFacet]) -> list[FieldFacet]:
    """Drop the identifier-like fields, unless that would leave nothing."""
    grouping = [facet for facet in facets if not is_identifier_like(facet)]
    return grouping or list(facets)


def normalize_field_path(field: str) -> tuple[str, ...]:
    """Split a user-typed field into its path, tolerating a ``parsed.`` prefix.

    ``level``, ``parsed.level``, and ``parsed.http.status`` all name the field a
    filter expression would name.
    """
    parts = tuple(part for part in field.split('.') if part)
    return parts[1:] if parts[:1] == ('parsed',) else parts


def discover_field_paths(parquet_paths: list[Path], *, limit: int) -> list[str]:
    """Rank the payload fields present across these files, most widespread first.

    Read from each file's schema rather than its rows, so the cost does not grow
    with the number of events. A field one group carries and another does not
    still ranks below one they share.
    """
    seen: Counter[str] = Counter()
    for path in parquet_paths:
        parsed = _payload_dtype(path)
        if isinstance(parsed, pl.Struct):
            seen.update(_leaf_paths(parsed, ()))
    return [path for path, _ in seen.most_common(limit)]


def _leaf_paths(dtype: Any, prefix: tuple[str, ...]) -> list[str]:
    if not isinstance(dtype, pl.Struct) or len(prefix) >= MAX_FIELD_DEPTH:
        return ['.'.join(prefix)] if prefix else []
    return [leaf for field in dtype.fields for leaf in _leaf_paths(field.dtype, (*prefix, field.name))]


def count_by_field(
    parquet_paths: list[Path],
    field: str,
    *,
    filter_node: FilterNode | None = None,
    top: int,
) -> FieldFacet:
    """Count the values of one payload field across several cached files.

    A file whose payload lacks the field contributes nothing rather than raising,
    matching how a field filter treats the same file. A native abort surfaces as
    :class:`EnginePanicError` and a rejected query as :class:`ValueError`.
    """
    path_parts = normalize_field_path(field)
    counts: Counter[str] = Counter()
    for path in parquet_paths:
        counts.update(_count_one_file(path, path_parts, filter_node=filter_node))
    present = sum(count for value, count in counts.items() if value != NULL_LABEL)
    return FieldFacet(
        path='.'.join(path_parts),
        values=tuple(FacetValue(value=value, count=count) for value, count in counts.most_common(top)),
        present=present,
        distinct=len(counts),
    )


def _count_one_file(
    parquet_path: Path,
    path_parts: tuple[str, ...],
    *,
    filter_node: FilterNode | None,
) -> Counter[str]:
    if not _file_has_field(parquet_path, path_parts):
        return Counter()
    reference = build_field_reference(list(path_parts))
    try:
        with duckdb.connect() as con:
            con.execute(f'SET threads = {duckdb_threads()}')
            described = con.execute('DESCRIBE SELECT * FROM read_parquet(?)', [str(parquet_path)]).fetchall()
            source, where = duckdb_source_and_where([row[0] for row in described], filter_node)
            sql = (
                f'SELECT CAST({reference} AS VARCHAR) AS facet_value, count(*) AS facet_count '  # noqa: S608
                f'FROM {source}{where} GROUP BY 1'
            )
            rows = con.execute(sql, [str(parquet_path)]).fetchall()
    except duckdb.Error as err:
        msg = f'Counting {".".join(path_parts)} failed: {err}'
        raise ValueError(msg) from err
    return Counter({(NULL_LABEL if value is None else str(value)): int(count) for value, count in rows})


def _payload_dtype(parquet_path: Path) -> Any:
    """Read one file's payload dtype, converting a native abort the way the engine does."""
    try:
        return pl.scan_parquet(str(parquet_path)).collect_schema().get('parsed')
    except BaseException as err:
        if not is_engine_panic(err):
            raise
        raise EnginePanicError(parquet_path, err) from err


def _file_has_field(parquet_path: Path, path_parts: tuple[str, ...]) -> bool:
    dtype: Any = _payload_dtype(parquet_path)
    for part in path_parts:
        if not isinstance(dtype, pl.Struct):
            return False
        fields = {field.name: field.dtype for field in dtype.fields}
        if part not in fields:
            return False
        dtype = fields[part]
    return True
