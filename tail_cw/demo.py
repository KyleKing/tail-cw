"""Offline demo mode: a synthetic dashboard with generated seed data.

Renders a plausible service dashboard (traffic, latency percentiles, error rate,
saturation, availability, and a recent-errors log panel) with no AWS calls, so
the TUI can be driven for screenshots and tried without credentials. The seed
data simulates a mid-window incident: a latency and error spike with a traffic
dip and recovery. Everything is a deterministic function of time so captures are
reproducible.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from tail_cw.aws.dashboards import Dashboard, parse_dashboard_body
from tail_cw.aws.events import LogEvent
from tail_cw.aws.metrics import MetricSeries
from tail_cw.aws.xray import XRaySpan, XRayTrace
from tail_cw.cache.storage import write_log_events_to_parquet

_STEP = timedelta(minutes=5)
DEMO_LOG_GROUP = 'demo/web-api'


def _demo_body() -> dict[str, Any]:
    return {
        'widgets': [
            {
                'type': 'text',
                'x': 0,
                'y': 0,
                'width': 24,
                'height': 2,
                'properties': {'markdown': '# web-api — service overview (demo)'},
            },
            {
                'type': 'metric',
                'x': 0,
                'y': 2,
                'width': 12,
                'height': 7,
                'properties': {
                    'title': 'Requests / 5 min',
                    'view': 'bar',
                    'stat': 'Sum',
                    'metrics': [['demo/web-api', 'RequestCount', {'id': 'requests'}]],
                },
            },
            {
                'type': 'metric',
                'x': 12,
                'y': 2,
                'width': 12,
                'height': 7,
                'properties': {
                    'title': '5xx error rate (%)',
                    'view': 'timeSeries',
                    'metrics': [
                        [{'expression': 'errors / requests * 100', 'label': '5xx error rate', 'id': 'error_rate'}]
                    ],
                },
            },
            {
                'type': 'metric',
                'x': 0,
                'y': 9,
                'width': 12,
                'height': 7,
                'properties': {
                    'title': 'Latency (ms)',
                    'view': 'timeSeries',
                    'metrics': [
                        ['demo/web-api', 'Latency', {'stat': 'p50', 'label': 'latency p50', 'id': 'lat_p50'}],
                        ['...', {'stat': 'p90', 'label': 'latency p90', 'id': 'lat_p90'}],
                        ['...', {'stat': 'p99', 'label': 'latency p99', 'id': 'lat_p99'}],
                    ],
                },
            },
            {
                'type': 'metric',
                'x': 12,
                'y': 9,
                'width': 12,
                'height': 7,
                'properties': {
                    'title': 'Saturation (%)',
                    'view': 'timeSeries',
                    'metrics': [
                        ['demo/web-api', 'CPUUtilization', {'stat': 'Average', 'label': 'cpu', 'id': 'cpu'}],
                        ['demo/web-api', 'MemoryUtilization', {'stat': 'Average', 'label': 'memory', 'id': 'memory'}],
                    ],
                },
            },
            {
                'type': 'metric',
                'x': 0,
                'y': 16,
                'width': 12,
                'height': 6,
                'properties': {
                    'title': 'Availability (%)',
                    'view': 'timeSeries',
                    'metrics': [
                        [
                            'demo/web-api',
                            'Availability',
                            {'stat': 'Average', 'label': 'availability', 'id': 'availability'},
                        ]
                    ],
                },
            },
            {
                'type': 'log',
                'x': 12,
                'y': 16,
                'width': 12,
                'height': 6,
                'properties': {
                    'title': 'Recent errors',
                    'view': 'table',
                    'query': f"SOURCE '{DEMO_LOG_GROUP}' | filter level = 'ERROR' | fields @timestamp, message",
                },
            },
        ],
    }


def demo_dashboard() -> Dashboard:
    """Return the synthetic demo dashboard parsed through the real parser."""
    return parse_dashboard_body('demo', json.dumps(_demo_body()))


def _timestamps(start: datetime, end: datetime) -> list[datetime]:
    steps = max(2, int((end - start) / _STEP))
    return [start + _STEP * i for i in range(steps + 1)]


def _incident_factor(fraction: float) -> float:
    """A bell-shaped bump peaking around 55% through the window."""
    return math.exp(-(((fraction - 0.55) / 0.09) ** 2))


def _shape_for(label: str, timestamps: list[datetime]) -> list[float]:
    key = label.lower()
    count = len(timestamps)
    values: list[float] = []
    for index in range(count):
        fraction = index / max(1, count - 1)
        incident = _incident_factor(fraction)
        diurnal = math.sin(fraction * math.pi)
        if 'request' in key:
            values.append(900 + 500 * diurnal - 400 * incident)
        elif 'error rate' in key or '5xx' in key:
            values.append(0.2 + 8.0 * incident)
        elif 'p99' in key:
            values.append(300 + 900 * incident)
        elif 'p90' in key:
            values.append(120 + 350 * incident)
        elif 'p50' in key or 'latency' in key:
            values.append(40 + 60 * incident)
        elif 'cpu' in key:
            values.append(38 + 48 * incident + 4 * diurnal)
        elif 'memory' in key:
            values.append(52 + 12 * fraction)
        elif 'avail' in key:
            values.append(99.95 - 3.0 * incident)
        else:
            values.append(50 + 25 * diurnal)
    return values


def demo_fetch_metrics(
    queries: Sequence[dict[str, Any]],
    start: datetime,
    end: datetime,
) -> list[MetricSeries]:
    """Generate deterministic series for the visible queries, ignoring AWS."""
    timestamps = _timestamps(start, end)
    series: list[MetricSeries] = []
    for query in queries:
        if not query.get('ReturnData', True):
            continue
        label = str(query.get('Label') or query.get('Id') or 'metric')
        series.append(
            MetricSeries(id=query['Id'], label=label, timestamps=timestamps, values=_shape_for(label, timestamps))
        )
    return series


def demo_log_events(start: datetime, end: datetime) -> list[LogEvent]:
    """Build the synthetic request log the offline demo reads."""
    timestamps = _timestamps(start, end)
    events: list[LogEvent] = []
    for index, moment in enumerate(timestamps):
        fraction = index / max(1, len(timestamps) - 1)
        is_error = _incident_factor(fraction) > 0.5  # ruff: ignore[magic-value-comparison]
        level = 'ERROR' if is_error else 'INFO'
        trace_id = f'trace-{1000 + index}'
        status = 503 if is_error else 200
        message = (
            f'{{"level":"{level}","trace_id":"{trace_id}","path":"/v1/orders","status":{status},'
            f'"latency_ms":{300 if is_error else 42},"message":"request completed"}}'
        )
        events.append(
            LogEvent(
                log_group=DEMO_LOG_GROUP,
                log_stream='demo-stream',
                timestamp=moment,
                message=message,
                ingestion_time=moment,
            ),
        )
    return events


def demo_resolve_logs(_log_group: str, start: datetime, end: datetime) -> Path | None:
    """Write seed log events to a Parquet file and return its path.

    The name carries the process id, and the file is written aside and moved into
    place: two processes sharing one path wrote over each other mid-write, and the
    reader then got a Parquet file with no footer.
    """
    output = Path(tempfile.gettempdir()) / f'tail-cw-demo-logs-{os.getpid()}.parquet'
    staged = output.with_suffix('.staging')
    write_log_events_to_parquet(demo_log_events(start, end), staged)
    staged.replace(output)
    return output


def demo_count_events(_log_group: str, start: datetime, end: datetime) -> int:
    """Count the synthetic ERROR-level events in the window, for the dive-candidate preview."""
    return sum(1 for event in demo_log_events(start, end) if '"level":"ERROR"' in event.message)


def demo_log_volume(_source: str, start: datetime, end: datetime) -> list[float]:
    """Synthetic error-log volume per bucket, peaking during the incident."""
    timestamps = _timestamps(start, end)
    return [round(2 + 60 * _incident_factor(index / max(1, len(timestamps) - 1))) for index in range(len(timestamps))]


def demo_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    """Return a fixed 6-hour demo window ending at ``now`` (default: current time)."""
    end = now if now is not None else datetime.now(tz=UTC)
    return end - timedelta(hours=6), end


DEMO_TRACE_ID = '1-6a89adb9-b279784d231d504c96c8815f'
"""The trace ``:xray`` draws offline. X-Ray shaped, so the pivot accepts it."""


def demo_xray_trace(trace_id: str, *, now: datetime | None = None) -> XRayTrace:
    """Build one synthetic trace with the shape a real one has.

    Deep enough to exercise the waterfall's geometry: a root that most of the time sits
    inside, two sibling calls where only one is slow, an inferred database segment, and a
    fault at the bottom.
    """
    origin = (now or datetime.now(tz=UTC)).replace(microsecond=0)

    def span(
        span_id: str,
        name: str,
        *,
        parent: str | None,
        offset_ms: int,
        length_ms: int,
        service: str = 'web-api',
        inferred: bool = False,
        fault: bool = False,
        sql: str | None = None,
    ) -> XRaySpan:
        start = origin + timedelta(milliseconds=offset_ms)
        return XRaySpan(
            trace_id=trace_id,
            span_id=span_id,
            parent_span_id=parent,
            name=name,
            start_time=start,
            end_time=start + timedelta(milliseconds=length_ms),
            service_name=service,
            origin='Database::SQL' if inferred else None,
            namespace='remote' if sql else None,
            is_fault=fault,
            is_error=False,
            is_throttle=False,
            is_inferred=inferred,
            http_status=503 if fault else None,
            sql_url=sql,
            error_message='connection reset by peer' if fault else None,
            annotations=(),
        )

    return XRayTrace(
        trace_id=trace_id,
        duration_seconds=1.24,
        limit_exceeded=False,
        spans=(
            span('a1a1a1a1a1a1a1a1', 'POST /v1/orders', parent=None, offset_ms=0, length_ms=1240),
            span('b2b2b2b2b2b2b2b2', 'auth.verify_token', parent='a1a1a1a1a1a1a1a1', offset_ms=6, length_ms=48),
            span('c3c3c3c3c3c3c3c3', 'orders.load_customer', parent='a1a1a1a1a1a1a1a1', offset_ms=60, length_ms=94),
            span(
                'd4d4d4d4d4d4d4d4',
                'query SelectCustomer',
                parent='c3c3c3c3c3c3c3c3',
                offset_ms=64,
                length_ms=86,
                sql='SELECT * FROM customer WHERE id = $1',
            ),
            span(
                'e5e5e5e5e5e5e5e5',
                'pool.acquire',
                parent='d4d4d4d4d4d4d4d4',
                offset_ms=64,
                length_ms=1,
                service='Database::SQL',
                inferred=True,
            ),
            span('f6f6f6f6f6f6f6f6', 'orders.reserve_stock', parent='a1a1a1a1a1a1a1a1', offset_ms=160, length_ms=1010),
            span(
                '0707070707070707',
                'inventory.reserve',
                parent='f6f6f6f6f6f6f6f6',
                offset_ms=170,
                length_ms=995,
                service='inventory',
                fault=True,
            ),
            span('1818181818181818', 'orders.emit_event', parent='a1a1a1a1a1a1a1a1', offset_ms=1180, length_ms=52),
        ),
    )
