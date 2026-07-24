"""CloudWatch metric fetching and console-shorthand translation.

CloudWatch dashboards store metric widgets in a compact ``metrics[]`` shorthand:
raw rows shaped ``[Namespace, MetricName, DimName, DimVal, ..., {options}]``,
positional ``.`` ditto references that copy the element at the same position
from the previous raw row, and metric-math ``expression`` rows. This module
translates that shorthand into ``GetMetricData`` ``MetricDataQueries`` and
fetches the resulting time series.

Expression rows reference source metrics by id, so ids supplied in the
dashboard are preserved; only rows without an id get a generated one.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from tail_cw.aws.client import build_client

DEFAULT_STAT = 'Average'
DITTO = '.'
_ID_PREFIX = 'q'


@dataclass(frozen=True)
class MetricSeries:
    """One fetched metric series: aligned timestamps and values.

    Attributes:
        id: The GetMetricData query id that produced this series.
        label: Human-readable series label.
        timestamps: Ascending datapoint timestamps (timezone-aware).
        values: Datapoint values aligned to timestamps.
    """

    id: str
    label: str
    timestamps: list[datetime]
    values: list[float]


def _row_options(row: Sequence[Any]) -> dict[str, Any]:
    if row and isinstance(row[-1], dict):
        return row[-1]
    return {}


def _is_expression_row(row: Sequence[Any]) -> bool:
    return bool(row) and isinstance(row[0], dict) and 'expression' in row[0]


def _positional_elements(row: Sequence[Any]) -> list[Any]:
    """Return the non-option positional elements of a raw metric row."""
    if row and isinstance(row[-1], dict):
        return list(row[:-1])
    return list(row)


def _resolve_ditto(positional: list[Any], previous: list[Any] | None) -> list[str]:
    """Replace ``.`` placeholders with the previous row's element at each index."""
    resolved: list[str] = []
    for index, element in enumerate(positional):
        if element == DITTO:
            if previous is None or index >= len(previous):
                msg = f'Ditto reference at position {index} has no previous value'
                raise ValueError(msg)
            resolved.append(str(previous[index]))
        else:
            resolved.append(str(element))
    return resolved


def _dimensions_from_positional(resolved: list[str]) -> list[dict[str, str]]:
    """Build Dimensions from positional elements after namespace and metric name."""
    pairs = resolved[2:]
    return [{'Name': pairs[i], 'Value': pairs[i + 1]} for i in range(0, len(pairs) - 1, 2)]


def build_metric_data_queries(
    metrics_shorthand: Sequence[Sequence[Any]],
    *,
    widget_stat: str | None,
    widget_period: int | None,
    default_period: int,
) -> list[dict[str, Any]]:
    """Translate console ``metrics[]`` shorthand into GetMetricData queries.

    Args:
        metrics_shorthand: The widget's ``metrics`` array from the dashboard JSON.
        widget_stat: Widget-level ``stat`` used when a row omits its own.
        widget_period: Widget-level ``period`` used when a row omits its own.
        default_period: Fallback period (seconds) when neither row nor widget set one.

    Returns:
        A list of ``MetricDataQuery`` dicts ready for ``get_metric_data``.
    """
    queries: list[dict[str, Any]] = []
    previous_positional: list[str] | None = None
    generated = 0

    for index, row in enumerate(metrics_shorthand):
        options = _row_options(row)
        visible = options.get('visible', True)
        provided_id = options.get('id')
        label = options.get('label')

        if _is_expression_row(row):
            expr = row[0]
            query: dict[str, Any] = {
                'Id': str(provided_id or f'{_ID_PREFIX}e{index}'),
                'Expression': str(expr['expression']),
                'ReturnData': bool(visible),
            }
            if label or expr.get('label'):
                query['Label'] = str(label or expr['label'])
            queries.append(query)
            continue

        positional = _positional_elements(row)
        resolved = _resolve_ditto(positional, previous_positional)
        previous_positional = resolved

        if provided_id is not None:
            query_id = str(provided_id)
        else:
            query_id = f'{_ID_PREFIX}{generated}'
            generated += 1

        stat = str(options.get('stat') or widget_stat or DEFAULT_STAT)
        period = int(options.get('period') or widget_period or default_period)
        metric_stat: dict[str, Any] = {
            'Metric': {
                'Namespace': resolved[0],
                'MetricName': resolved[1],
                'Dimensions': _dimensions_from_positional(resolved),
            },
            'Period': period,
            'Stat': stat,
        }
        query = {'Id': query_id, 'MetricStat': metric_stat, 'ReturnData': bool(visible)}
        if label:
            query['Label'] = str(label)
        queries.append(query)

    return queries


def fetch_metric_data(
    queries: Sequence[dict[str, Any]],
    start_time: datetime,
    end_time: datetime,
    *,
    profile_name: str | None = None,
    region_name: str | None = None,
    client: Any | None = None,
) -> list[MetricSeries]:
    """Fetch metric series for the given queries via GetMetricData.

    Only series whose query has ``ReturnData`` true are returned (source metrics
    feeding an expression are excluded). Paginates across ``NextToken`` and
    concatenates datapoints per id. A boto3 CloudWatch client may be injected
    for testing; otherwise one is built from the profile and region.

    Raises:
        ValueError: If queries is empty.
    """
    if not queries:
        msg = 'At least one metric query is required'
        raise ValueError(msg)

    cw = (
        client if client is not None else build_client('cloudwatch', region_name=region_name, profile_name=profile_name)
    )
    labels: dict[str, str] = {}
    ordered_ids: list[str] = []
    timestamps: dict[str, list[datetime]] = {}
    values: dict[str, list[float]] = {}

    next_token: str | None = None
    while True:
        kwargs: dict[str, Any] = {
            'MetricDataQueries': list(queries),
            'StartTime': start_time,
            'EndTime': end_time,
            'ScanBy': 'TimestampAscending',
        }
        if next_token is not None:
            kwargs['NextToken'] = next_token
        response = cw.get_metric_data(**kwargs)
        for result in response.get('MetricDataResults', []):
            result_id = result['Id']
            if result_id not in timestamps:
                ordered_ids.append(result_id)
                timestamps[result_id] = []
                values[result_id] = []
                labels[result_id] = result.get('Label', result_id)
            timestamps[result_id].extend(result.get('Timestamps', []))
            values[result_id].extend(result.get('Values', []))
        next_token = response.get('NextToken')
        if next_token is None:
            break

    return [
        MetricSeries(id=result_id, label=labels[result_id], timestamps=timestamps[result_id], values=values[result_id])
        for result_id in ordered_ids
    ]
