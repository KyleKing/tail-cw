"""Lay an X-Ray trace out as a waterfall, as pure geometry over the spans.

[ADR 0012](https://tail-cw.kyleking.me/docs/adr/0012-export-traces-instead-of-drawing-them/)
rejects a waterfall drawn from log timestamps, because a log line carries one timestamp
and no parent. X-Ray segment documents carry both, so the picture below is the service's own
account of what waited on what rather than an inference from when lines were written.

Nothing here imports Textual. The layout is a list of rows with fractional offsets, so
the screen only has to pick a character width.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from tail_cw.aws.xray import XRaySpan, XRayTrace

BAR_FULL = '█'
BAR_EMPTY = ' '
_MIN_BAR_CHARS = 1


@dataclass(frozen=True)
class WaterfallRow:
    """One span placed against the trace's own start and width.

    Attributes:
        span: The span this row draws.
        depth: Nesting depth, zero for a root.
        offset: Where the span starts, as a fraction of the trace's width.
        extent: How much of the trace's width the span covers, as a fraction.
        on_slowest_chain: The span is on the chain returned by :func:`slowest_chain`.
    """

    span: XRaySpan
    depth: int
    offset: float
    extent: float
    on_slowest_chain: bool


def _span_end(span: XRaySpan) -> datetime:
    return span.end_time if span.end_time is not None else span.start_time


def _children_by_parent(trace: XRayTrace) -> dict[str | None, list[XRaySpan]]:
    """Index spans by parent, treating a parent outside the trace as a root.

    X-Ray truncates a large trace, so a span whose parent was dropped would otherwise
    disappear from the picture entirely.
    """
    known = {span.span_id for span in trace.spans}
    children: dict[str | None, list[XRaySpan]] = defaultdict(list)
    for span in trace.spans:
        parent = span.parent_span_id if span.parent_span_id in known else None
        children[parent].append(span)
    for siblings in children.values():
        siblings.sort(key=lambda span: (span.start_time, span.name))
    return children


def slowest_chain(trace: XRayTrace) -> tuple[str, ...]:
    """Follow the longest child at every step, from the widest root down.

    A heuristic, not a critical path: siblings can run in parallel, so the chain names
    where the time went rather than proving nothing else could have been the cause.
    """
    children = _children_by_parent(trace)
    roots = children.get(None, [])
    if not roots:
        return ()
    current = max(roots, key=lambda span: span.duration_ms or 0.0)
    chain = [current.span_id]
    while siblings := children.get(current.span_id):
        current = max(siblings, key=lambda span: span.duration_ms or 0.0)
        chain.append(current.span_id)
    return tuple(chain)


def waterfall_rows(trace: XRayTrace) -> list[WaterfallRow]:
    """Place every span against the trace's full width, parents before their children.

    A trace with no measurable width (every span instantaneous) gets full-width rows
    rather than zero-width ones, so the names stay readable.
    """
    if not trace.spans:
        return []
    children = _children_by_parent(trace)
    origin = min(span.start_time for span in trace.spans)
    finish = max(_span_end(span) for span in trace.spans)
    width = (finish - origin).total_seconds()
    chain = set(slowest_chain(trace))

    rows: list[WaterfallRow] = []

    def place(span: XRaySpan, depth: int) -> None:
        start = (span.start_time - origin).total_seconds()
        rows.append(
            WaterfallRow(
                span=span,
                depth=depth,
                offset=start / width if width else 0.0,
                extent=(_span_end(span) - span.start_time).total_seconds() / width if width else 1.0,
                on_slowest_chain=span.span_id in chain,
            ),
        )
        for child in children.get(span.span_id, ()):
            place(child, depth + 1)

    for root in children.get(None, ()):
        place(root, 0)
    return rows


def render_bar(row: WaterfallRow, *, width: int) -> str:
    """Draw one row's bar at ``width`` characters, padded to that width.

    A span narrower than a character still gets one, because a span that took no
    measurable time is not the same thing as a span that is not there.
    """
    if width <= 0:
        return ''
    start = min(int(row.offset * width), width - _MIN_BAR_CHARS)
    length = max(_MIN_BAR_CHARS, round(row.extent * width))
    length = min(length, width - start)
    return f'{BAR_EMPTY * start}{BAR_FULL * length}'.ljust(width)


def indented_name(row: WaterfallRow, *, width: int) -> str:
    """The span name at its depth, truncated from the left to keep the tail readable."""
    prefix = '  ' * row.depth
    available = max(0, width - len(prefix))
    name = row.span.name
    if len(name) > available:
        name = f'…{name[-(available - 1) :]}' if available > 1 else ''
    return f'{prefix}{name}'
