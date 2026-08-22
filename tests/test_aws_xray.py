"""Cover the segment-document parsing and paging in :mod:`tail_cw.aws.xray`.

The documents here are trimmed copies of real ``BatchGetTraces`` responses from the
production account, which is where the inferred-segment behaviour came from.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from tail_cw.aws.xray import (
    TRACE_IDS_PER_REQUEST,
    as_xray_trace_id,
    batch_get_traces,
    flatten_segment,
    get_trace_summaries,
    parse_trace,
)

TRACE_ID = '1-96efc44a-447900ff4e2e2ccec98c47f1'
_START = 1787407556.764126


def _segment(**overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        'id': 'aaaaaaaaaaaaaaa1',
        'name': 'hatchet.run/SendMessage',
        'start_time': _START,
        'end_time': _START + 0.05,
        'trace_id': TRACE_ID,
        'metadata': {'default': {'otel.resource.service.name': 'hatchet-server'}},
    }
    document.update(overrides)
    return document


class _FakePaginator:
    def __init__(self, pages: Sequence[dict[str, Any]], calls: list[dict[str, Any]]) -> None:
        self.pages = pages
        self.calls = calls

    async def _iterate(self, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        self.calls.append(kwargs)
        for page in self.pages:
            yield page

    def paginate(self, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        return self._iterate(**kwargs)


class _FakeXRay:
    """Answers both paginators, recording what each was asked for."""

    def __init__(self, *, summaries: Sequence[dict[str, Any]] = (), traces: Sequence[dict[str, Any]] = ()) -> None:
        self.summaries = summaries
        self.traces = traces
        self.calls: list[dict[str, Any]] = []

    def get_paginator(self, name: str) -> _FakePaginator:
        if name == 'get_trace_summaries':
            return _FakePaginator([{'TraceSummaries': list(self.summaries)}], self.calls)
        return _FakePaginator([{'Traces': list(self.traces)}], self.calls)


def test_a_subsegment_inherits_the_service_its_parent_named() -> None:
    """Only the top segment carries the OTel resource name, so children would be nameless."""
    document = _segment(subsegments=[{'id': 'bbbbbbbbbbbbbbb2', 'name': 'query Users', 'start_time': _START}])

    spans = list(flatten_segment(document, trace_id=TRACE_ID))

    assert [span.service_name for span in spans] == ['hatchet-server', 'hatchet-server']
    assert spans[1].parent_span_id == spans[0].span_id, 'nesting is the only parent link a subsegment has'
    assert spans[1].end_time is None
    assert spans[1].duration_ms is None, 'a span still in progress has no duration to report'


def test_an_inferred_segment_reads_as_its_resource_not_as_the_query_that_reached_it() -> None:
    """X-Ray synthesizes one segment per downstream resource, named after the caller's call."""
    document = {
        'id': 'ccccccccccccccc3',
        'name': 'query SELECT PG_NOTIFY12',
        'start_time': _START,
        'end_time': _START + 0.01,
        'parent_id': 'aaaaaaaaaaaaaaa1',
        'inferred': True,
        'origin': 'Database::SQL',
        'sql': {'url': 'SELECT PG_NOTIFY($1,$2)'},
    }

    span = next(iter(flatten_segment(document, trace_id=TRACE_ID)))

    assert span.service_name == 'Database::SQL'
    assert span.is_inferred
    assert span.sql_url == 'SELECT PG_NOTIFY($1,$2)'
    assert span.parent_span_id == 'aaaaaaaaaaaaaaa1', 'a top-level segment links through parent_id'


def test_a_trace_sorts_its_spans_and_survives_an_undecodable_document() -> None:
    late = _segment(id='ddddddddddddddd4', start_time=_START + 1)
    payload = {
        'Id': TRACE_ID,
        'Duration': 1.05,
        'Segments': [
            {'Document': json.dumps(late)},
            {'Document': '{not json'},
            {'Document': json.dumps(_segment())},
        ],
    }

    trace = parse_trace(payload)

    assert [span.span_id for span in trace.spans] == ['aaaaaaaaaaaaaaa1', 'ddddddddddddddd4']
    assert trace.service_names == ('hatchet-server',)
    assert trace.error_count == 0


def test_a_fault_with_a_cause_reports_the_exception_message() -> None:
    document = _segment(
        fault=True, cause={'exceptions': [{'message': 'connection reset'}]}, http={'response': {'status': 502}}
    )

    span = next(iter(flatten_segment(document, trace_id=TRACE_ID)))

    assert span.is_fault
    assert span.error_message == 'connection reset'
    assert span.http_status == 502
    assert parse_trace({'Id': TRACE_ID, 'Segments': [{'Document': json.dumps(document)}]}).error_count == 1


async def test_summaries_report_utc_and_the_expression_reaches_the_api() -> None:
    """``StartTime`` arrives parsed by botocore, stamped with the machine's local zone."""
    local = datetime(2026, 8, 22, 8, 15, tzinfo=timezone(timedelta(hours=-6)))
    client = _FakeXRay(
        summaries=[
            {
                'Id': TRACE_ID,
                'StartTime': local,
                'Duration': 3.2,
                'HasFault': True,
                'ServiceIds': [{'Name': 'irm-api'}, {'Name': 'irm-api'}],
                'EntryPoint': {'Name': 'irm-api'},
                'Http': {'HttpMethod': 'POST', 'HttpStatus': 502},
            },
        ],
    )

    summaries = [
        summary
        async for summary in get_trace_summaries(
            client,
            start_time=local,
            end_time=local + timedelta(hours=1),
            filter_expression='responsetime > 3',
        )
    ]

    assert len(summaries) == 1
    assert summaries[0].start_time is not None
    assert summaries[0].start_time.utcoffset() == timedelta(0)
    assert summaries[0].start_time == local, 'the instant must not move, only the zone it prints in'
    assert summaries[0].service_names == ('irm-api',), 'a trace names the same service once per segment'
    assert summaries[0].http_status == 502
    assert client.calls[0]['FilterExpression'] == 'responsetime > 3'


async def test_a_batch_asks_for_five_ids_at_a_time() -> None:
    """``BatchGetTraces`` rejects a longer list, and a window is hundreds of ids."""
    trace_ids = [f'1-0000000{index}-{index:032x}' for index in range(12)]
    client = _FakeXRay(traces=[{'Id': TRACE_ID, 'Segments': [{'Document': json.dumps(_segment())}]}])

    traces = await batch_get_traces(client, trace_ids)

    requested = [call['TraceIds'] for call in client.calls]
    assert sorted(len(chunk) for chunk in requested) == [2, 5, 5]
    assert sorted(id_ for chunk in requested for id_ in chunk) == sorted(trace_ids)
    assert len(traces) == len(requested), 'every chunk contributes its traces'
    assert TRACE_IDS_PER_REQUEST == 5


async def test_no_ids_asks_the_api_nothing() -> None:
    client = _FakeXRay()

    assert await batch_get_traces(client, []) == []
    assert client.calls == []


async def test_batches_overlap_rather_than_running_one_after_another() -> None:
    """Each request is a serial round trip, so a barrier of two proves they overlap."""
    entered = asyncio.Semaphore(0)
    release = asyncio.Event()

    class _BlockingXRay(_FakeXRay):
        def get_paginator(self, name: str) -> Any:
            del name
            outer = self

            async def blocked(**kwargs: Any) -> AsyncIterator[dict[str, Any]]:
                outer.calls.append(kwargs)
                entered.release()
                await release.wait()
                yield {'Traces': []}

            class _Blocking:
                paginate = staticmethod(blocked)

            return _Blocking()

    client = _BlockingXRay()
    task = asyncio.create_task(batch_get_traces(client, [f'trace-{index}' for index in range(10)], concurrency=2))
    await asyncio.wait_for(entered.acquire(), timeout=2)
    await asyncio.wait_for(entered.acquire(), timeout=2)
    release.set()

    assert await asyncio.wait_for(task, timeout=2) == []


_NOW = datetime(2026, 8, 22, 14, 30, tzinfo=UTC)
_DASHED = '1-6a89ad51-596cd68140b3de150546b2a7'


@pytest.mark.parametrize(
    ('value', 'expected'),
    [
        (_DASHED, _DASHED),
        ('1-6A89AD51-596CD68140B3DE150546B2A7', _DASHED),
        (f'  {_DASHED}  ', _DASHED),
        ('6a89ad51596cd68140b3de150546b2a7', _DASHED),
        ('7f3c1e9a4b2d', None),
        ('1-6a89ad51-596cd68140b3de150546b2', None),
        ('1-6a89ad51-zz6cd68140b3de150546b2a7', None),
        ('', None),
    ],
)
def test_a_w3c_id_from_a_log_line_reads_as_the_dashed_id_x_ray_answers_to(
    value: str,
    expected: str | None,
) -> None:
    """Our services log the W3C form, so checking for the dashes rejects every log line."""
    assert as_xray_trace_id(value, now=_NOW) == expected


def test_a_32_digit_id_from_another_generator_is_not_mistaken_for_an_x_ray_one() -> None:
    """The leading eight digits are an epoch, and only X-Ray's generator puts one there."""
    stale = '1e2f3a4b' + 'c' * 24
    future = f'{int((_NOW + timedelta(days=2)).timestamp()):08x}' + 'c' * 24

    assert as_xray_trace_id(stale, now=_NOW) is None
    assert as_xray_trace_id(future, now=_NOW) is None
    assert as_xray_trace_id(f'{int(_NOW.timestamp()):08x}' + 'c' * 24, now=_NOW) is not None
