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
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from tail_cw.aws.client import LogEvent
from tail_cw.aws.dashboards import Dashboard, parse_dashboard_body
from tail_cw.aws.metrics import MetricSeries
from tail_cw.cache.storage import write_log_events_to_parquet

_STEP = timedelta(minutes=5)
_DEMO_LOG_GROUP = 'demo/web-api'


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
                    'query': f"SOURCE '{_DEMO_LOG_GROUP}' | filter level = 'ERROR' | fields @timestamp, message",
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


def _demo_log_events(start: datetime, end: datetime) -> list[LogEvent]:
    timestamps = _timestamps(start, end)
    events: list[LogEvent] = []
    for index, moment in enumerate(timestamps):
        fraction = index / max(1, len(timestamps) - 1)
        is_error = _incident_factor(fraction) > 0.5  # noqa: PLR2004
        level = 'ERROR' if is_error else 'INFO'
        trace_id = f'trace-{1000 + index}'
        status = 503 if is_error else 200
        message = (
            f'{{"level":"{level}","trace_id":"{trace_id}","path":"/v1/orders","status":{status},'
            f'"latency_ms":{300 if is_error else 42},"message":"request completed"}}'
        )
        events.append(
            LogEvent(
                log_group=_DEMO_LOG_GROUP,
                log_stream='demo-stream',
                timestamp=moment,
                message=message,
                event_id=f'demo-{index}',
                ingestion_time=moment,
            ),
        )
    return events


def demo_resolve_logs(_log_group: str, start: datetime, end: datetime) -> Path | None:
    """Write seed log events to a Parquet file and return its path."""
    output = Path(tempfile.gettempdir()) / 'tail-cw-demo-logs.parquet'
    write_log_events_to_parquet(_demo_log_events(start, end), output)
    return output


def demo_log_volume(_source: str, start: datetime, end: datetime) -> list[float]:
    """Synthetic error-log volume per bucket, peaking during the incident."""
    timestamps = _timestamps(start, end)
    return [round(2 + 60 * _incident_factor(index / max(1, len(timestamps) - 1))) for index in range(len(timestamps))]


def demo_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    """Return a fixed 6-hour demo window ending at ``now`` (default: current time)."""
    end = now if now is not None else datetime.now(tz=UTC)
    return end - timedelta(hours=6), end
