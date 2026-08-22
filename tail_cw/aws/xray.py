"""Read traces from X-Ray, which is the only source here that knows what waited on what.

Our services log ``trace_id`` and ``span_id`` and no ``parent_span_id`` anywhere, so a
hierarchy cannot be recovered from log lines
([ADR 0012](../../docs/docs/adr/0012-export-traces-instead-of-drawing-them.md)). X-Ray
segment documents carry ``parent_id`` and nest their children under ``subsegments``, so
they answer the question log lines cannot.

``GetTraceSummaries`` is the discovery call and returns no timing detail per span;
``BatchGetTraces`` returns the documents and takes five trace ids per request.

Both are billed per trace, at :data:`COST_PER_MILLION_TRACES` with the first million a
month free. A filter expression does not make a query cheaper: ``TracesProcessedCount``
is documented as "the total number of traces processed, including traces that did not
match the specified filter expression", so only a shorter window reduces the bill.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

TRACE_IDS_PER_REQUEST = 5
"""``BatchGetTraces`` rejects a longer list."""

DEFAULT_TRACE_CONCURRENCY = 4
COST_PER_MILLION_TRACES = 0.50
"""USD per million traces scanned or retrieved, past the free million a month."""

_TRACES_PER_MILLION = 1_000_000
_MILLISECONDS_PER_SECOND = 1000.0
_SERVICE_NAME_KEYS = ('otel.resource.service.name', 'service.name')


@dataclass(frozen=True)
class XRayTraceSummary:
    """One trace as ``GetTraceSummaries`` describes it, before any document is read.

    Attributes:
        trace_id: X-Ray trace identifier, ``1-<hex>-<hex>``.
        start_time: When the trace began, or None when X-Ray omits it.
        duration_seconds: Wall time the trace covers.
        response_time_seconds: Time until the entry point responded, which is shorter
            than the duration whenever work continued after the response.
        has_fault: A 5xx or an unhandled exception occurred.
        has_error: A 4xx occurred.
        has_throttle: A request was throttled.
        is_partial: Segments were still arriving when X-Ray answered.
        service_names: Distinct service names the trace touched.
        entry_point: Name of the service that received the request.
        http_method: Request method, when the entry point recorded one.
        http_url: Request URL, when the entry point recorded one.
        http_status: Response status, when the entry point recorded one.
    """

    trace_id: str
    start_time: datetime | None
    duration_seconds: float | None
    response_time_seconds: float | None
    has_fault: bool
    has_error: bool
    has_throttle: bool
    is_partial: bool
    service_names: tuple[str, ...]
    entry_point: str | None
    http_method: str | None
    http_url: str | None
    http_status: int | None


@dataclass(frozen=True)
class XRaySpan:
    """One segment or subsegment, flattened out of the document tree.

    Attributes:
        trace_id: The trace this span belongs to.
        span_id: The segment's own id, unique inside the trace.
        parent_span_id: The enclosing segment, from ``parent_id`` or from the nesting.
        name: Operation name.
        start_time: When the span opened.
        end_time: When it closed, or None while it is still in progress.
        service_name: Resolved service, inherited by a subsegment from its parent.
        origin: What kind of resource X-Ray inferred, such as ``Database::SQL``.
        namespace: ``remote`` for a downstream call, ``aws`` for an AWS service.
        is_fault: A 5xx or unhandled exception, per the document's ``fault``.
        is_error: A 4xx, per the document's ``error``.
        is_throttle: The call was throttled.
        is_inferred: X-Ray synthesized this span rather than receiving it, which means
            its timings bound the caller's view of the work, not the work itself.
        http_status: Response status recorded on this span.
        sql_url: The statement for a SQL span, which is where the operation name is
            already truncated by the instrumentation.
        error_message: First exception message from ``cause``.
        annotations: Indexed key/value pairs, rendered as strings.
    """

    trace_id: str
    span_id: str
    parent_span_id: str | None
    name: str
    start_time: datetime
    end_time: datetime | None
    service_name: str
    origin: str | None
    namespace: str | None
    is_fault: bool
    is_error: bool
    is_throttle: bool
    is_inferred: bool
    http_status: int | None
    sql_url: str | None
    error_message: str | None
    annotations: tuple[tuple[str, str], ...]

    @property
    def duration_ms(self) -> float | None:
        """Wall time in milliseconds, or None while the span is still open."""
        if self.end_time is None:
            return None
        return (self.end_time - self.start_time).total_seconds() * _MILLISECONDS_PER_SECOND


@dataclass(frozen=True)
class XRayTrace:
    """One trace's spans, in start order.

    Attributes:
        trace_id: X-Ray trace identifier.
        duration_seconds: Wall time X-Ray reports for the trace.
        limit_exceeded: X-Ray truncated the segment list, so spans are missing.
        spans: Every segment and subsegment, earliest first.
    """

    trace_id: str
    duration_seconds: float | None
    limit_exceeded: bool
    spans: tuple[XRaySpan, ...]

    @property
    def error_count(self) -> int:
        """Spans that recorded a 4xx or a 5xx."""
        return sum(1 for span in self.spans if span.is_error or span.is_fault)

    @property
    def service_names(self) -> tuple[str, ...]:
        """Distinct services in first-seen order."""
        seen = dict.fromkeys(span.service_name for span in self.spans)
        return tuple(seen)


@dataclass(frozen=True)
class TraceSummaryPage:
    """One page of summaries and what X-Ray charged to find it.

    Attributes:
        summaries: The traces on this page that matched.
        traces_processed: Traces X-Ray scanned to build the page, matched or not. This
            is the billed quantity, so it is worth reporting even when the page is empty.
    """

    summaries: tuple[XRayTraceSummary, ...]
    traces_processed: int


def scan_cost_usd(traces_processed: int) -> float:
    """What a scan of this many traces costs, ignoring the monthly free million."""
    return traces_processed / _TRACES_PER_MILLION * COST_PER_MILLION_TRACES


def _epoch_to_datetime(value: Any) -> datetime | None:
    """X-Ray stamps a document with fractional epoch seconds, not a parsed timestamp."""
    if not isinstance(value, (int, float)):
        return None
    return datetime.fromtimestamp(float(value), tz=UTC)


def _service_name(document: Mapping[str, Any]) -> str | None:
    metadata = document.get('metadata')
    default = metadata.get('default') if isinstance(metadata, Mapping) else None
    if isinstance(default, Mapping):
        for key in _SERVICE_NAME_KEYS:
            value = default.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _resolve_service(document: Mapping[str, Any], inherited: str | None) -> str:
    """Name the service a span belongs to, which the document rarely states outright.

    An inferred segment is named after the call that reached it, so half a trace would
    read as one service per SQL statement. Its ``origin`` is the resource X-Ray decided
    it was, which is the only honest swimlane available for it.
    """
    named = _service_name(document)
    if named:
        return named
    if inherited:
        return inherited
    origin = document.get('origin')
    if document.get('inferred') and isinstance(origin, str) and origin:
        return origin
    return str(document.get('name', 'unknown'))


def _error_message(document: Mapping[str, Any]) -> str | None:
    cause = document.get('cause')
    if not isinstance(cause, Mapping):
        return None
    exceptions = cause.get('exceptions')
    if isinstance(exceptions, Sequence) and exceptions and isinstance(exceptions[0], Mapping):
        message = exceptions[0].get('message')
        if isinstance(message, str):
            return message
    return None


def _http_status(document: Mapping[str, Any]) -> int | None:
    http = document.get('http')
    response = http.get('response') if isinstance(http, Mapping) else None
    status = response.get('status') if isinstance(response, Mapping) else None
    return status if isinstance(status, int) else None


def _sql_url(document: Mapping[str, Any]) -> str | None:
    sql = document.get('sql')
    url = sql.get('url') if isinstance(sql, Mapping) else None
    return url if isinstance(url, str) else None


def _annotations(document: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    annotations = document.get('annotations')
    if not isinstance(annotations, Mapping):
        return ()
    return tuple((key, str(value)) for key, value in sorted(annotations.items()))


def _to_span(
    document: Mapping[str, Any],
    *,
    trace_id: str,
    parent_span_id: str | None,
    service_name: str,
) -> XRaySpan | None:
    span_id = document.get('id')
    start_time = _epoch_to_datetime(document.get('start_time'))
    if not isinstance(span_id, str) or start_time is None:
        return None
    return XRaySpan(
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=parent_span_id,
        name=str(document.get('name', span_id)),
        start_time=start_time,
        end_time=_epoch_to_datetime(document.get('end_time')),
        service_name=service_name,
        origin=document.get('origin'),
        namespace=document.get('namespace'),
        is_fault=bool(document.get('fault')),
        is_error=bool(document.get('error')),
        is_throttle=bool(document.get('throttle')),
        is_inferred=bool(document.get('inferred')),
        http_status=_http_status(document),
        sql_url=_sql_url(document),
        error_message=_error_message(document),
        annotations=_annotations(document),
    )


def flatten_segment(
    document: Mapping[str, Any],
    *,
    trace_id: str,
    parent_span_id: str | None = None,
    inherited_service: str | None = None,
) -> Iterator[XRaySpan]:
    """Walk one segment document and its nested subsegments, parents before children.

    A subsegment names no service of its own, so it inherits the enclosing segment's.
    Without that, every SQL call would read as its own service and the swimlanes would
    be one per query.

    Yields:
        XRaySpan records, each parent before its children.
    """
    service_name = _resolve_service(document, inherited_service)
    span = _to_span(
        document,
        trace_id=trace_id,
        parent_span_id=parent_span_id or document.get('parent_id'),
        service_name=service_name,
    )
    if span is None:
        return
    yield span
    children = document.get('subsegments')
    if not isinstance(children, Sequence):
        return
    for child in children:
        if isinstance(child, Mapping):
            yield from flatten_segment(
                child, trace_id=trace_id, parent_span_id=span.span_id, inherited_service=service_name
            )


def parse_trace(payload: Mapping[str, Any]) -> XRayTrace:
    """Build a trace from one ``BatchGetTraces`` entry, decoding each segment document."""
    trace_id = str(payload.get('Id', ''))
    spans: list[XRaySpan] = []
    for segment in payload.get('Segments', []):
        document = segment.get('Document')
        if not isinstance(document, str):
            continue
        try:
            decoded = json.loads(document)
        except ValueError:
            continue
        if isinstance(decoded, Mapping):
            spans.extend(flatten_segment(decoded, trace_id=trace_id))
    spans.sort(key=lambda span: span.start_time)
    return XRayTrace(
        trace_id=trace_id,
        duration_seconds=payload.get('Duration'),
        limit_exceeded=bool(payload.get('LimitExceeded')),
        spans=tuple(spans),
    )


def _to_summary(payload: Mapping[str, Any]) -> XRayTraceSummary:
    http = payload.get('Http')
    http = http if isinstance(http, Mapping) else {}
    services = payload.get('ServiceIds', [])
    names = dict.fromkeys(
        str(service['Name']) for service in services if isinstance(service, Mapping) and service.get('Name')
    )
    entry = payload.get('EntryPoint')
    return XRayTraceSummary(
        trace_id=str(payload.get('Id', '')),
        start_time=_start_time(payload.get('StartTime')),
        duration_seconds=payload.get('Duration'),
        response_time_seconds=payload.get('ResponseTime'),
        has_fault=bool(payload.get('HasFault')),
        has_error=bool(payload.get('HasError')),
        has_throttle=bool(payload.get('HasThrottle')),
        is_partial=bool(payload.get('IsPartial')),
        service_names=tuple(names),
        entry_point=str(entry['Name']) if isinstance(entry, Mapping) and entry.get('Name') else None,
        http_method=http.get('HttpMethod'),
        http_url=http.get('HttpURL'),
        http_status=http.get('HttpStatus'),
    )


def _start_time(value: Any) -> datetime | None:
    """``StartTime`` arrives parsed by botocore, in the machine's zone."""
    if isinstance(value, datetime):
        return value.astimezone(UTC)
    return _epoch_to_datetime(value)


async def iter_trace_summary_pages(
    client: Any,
    *,
    start_time: datetime,
    end_time: datetime,
    filter_expression: str | None = None,
    sampling: bool = False,
) -> AsyncIterator[TraceSummaryPage]:
    """Stream trace summaries a page at a time, newest page first as X-Ray returns them.

    Pages rather than records, because the cost of the query is reported per page and a
    caller that walks away early needs to know what it already spent.

    Args:
        client: An open X-Ray client, from :meth:`ClientPool.client`.
        start_time: Window start.
        end_time: Window end.
        filter_expression: An X-Ray filter expression, such as ``service("api")`` or
            ``responsetime > 3``. Server-side, so it narrows what comes back over the
            wire, but not what the query is billed for.
        sampling: Ask X-Ray for a representative sample instead of every trace.

    Yields:
        TraceSummaryPage records in the order the API returns them.
    """
    kwargs: dict[str, Any] = {'StartTime': start_time, 'EndTime': end_time, 'Sampling': sampling}
    if filter_expression:
        kwargs['FilterExpression'] = filter_expression
    paginator = client.get_paginator('get_trace_summaries')
    async for page in paginator.paginate(**kwargs):
        yield TraceSummaryPage(
            summaries=tuple(_to_summary(summary) for summary in page.get('TraceSummaries', [])),
            traces_processed=int(page.get('TracesProcessedCount', 0)),
        )


async def get_trace_summaries(
    client: Any,
    *,
    start_time: datetime,
    end_time: datetime,
    filter_expression: str | None = None,
    sampling: bool = False,
) -> AsyncIterator[XRayTraceSummary]:
    """Flatten :func:`iter_trace_summary_pages` for a caller that does not track cost.

    Yields:
        XRayTraceSummary records in the order the API returns them.
    """
    pages = iter_trace_summary_pages(
        client,
        start_time=start_time,
        end_time=end_time,
        filter_expression=filter_expression,
        sampling=sampling,
    )
    async for page in pages:
        for summary in page.summaries:
            yield summary


async def _traces_for_chunk(client: Any, trace_ids: Sequence[str]) -> list[XRayTrace]:
    paginator = client.get_paginator('batch_get_traces')
    traces: list[XRayTrace] = []
    async for page in paginator.paginate(TraceIds=list(trace_ids)):
        traces.extend(parse_trace(payload) for payload in page.get('Traces', []))
    return traces


def _chunk(values: Sequence[str], size: int) -> list[Sequence[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


async def batch_get_traces(
    client: Any,
    trace_ids: Sequence[str],
    *,
    concurrency: int = DEFAULT_TRACE_CONCURRENCY,
) -> list[XRayTrace]:
    """Fetch full segment documents for every id, five per request.

    Requests overlap because each one is a serial round trip and a window worth of
    traces is hundreds of ids. The semaphore is built here rather than held at module
    level, so it binds to the loop that is running.
    """
    if not trace_ids:
        return []
    limiter = asyncio.Semaphore(max(1, concurrency))

    async def fetch(chunk: Sequence[str]) -> list[XRayTrace]:
        async with limiter:
            return await _traces_for_chunk(client, chunk)

    async with asyncio.TaskGroup() as group:
        tasks = [group.create_task(fetch(chunk)) for chunk in _chunk(trace_ids, TRACE_IDS_PER_REQUEST)]
    return [trace for task in tasks for trace in task.result()]
