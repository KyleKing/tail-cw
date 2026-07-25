"""CloudWatch dashboard listing, fetching, and parsing.

Reads dashboards through ``ListDashboards`` and ``GetDashboard`` and parses the
``DashboardBody`` JSON into a typed widget model shared by the console-import
path and the tail-cw-native config path. Unknown widget types are preserved as
``UnknownWidget`` so one exotic widget never fails the whole dashboard.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_DEFAULT_WIDTH = 6
_DEFAULT_HEIGHT = 6
_SOURCE_RE = re.compile(r"SOURCE\s+'([^']+)'")
_DIMENSION_NAMES = frozenset(
    {'ApiId', 'ClusterName', 'DBInstanceIdentifier', 'FunctionName', 'LoadBalancer', 'ServiceName'}
)


@dataclass(frozen=True)
class WidgetLayout:
    """Grid placement of a widget in the 24-column dashboard grid."""

    x: int = 0
    y: int = 0
    width: int = _DEFAULT_WIDTH
    height: int = _DEFAULT_HEIGHT


@dataclass(frozen=True)
class MetricWidget:
    """A metric graph or single-value widget."""

    layout: WidgetLayout
    title: str
    view: str
    metrics: list[list[Any]]
    region: str | None = None
    period: int | None = None
    stat: str | None = None
    sparkline: bool = False


@dataclass(frozen=True)
class LogWidget:
    """A Logs Insights widget carrying a query string."""

    layout: WidgetLayout
    title: str
    query: str
    view: str = 'table'
    region: str | None = None


@dataclass(frozen=True)
class TextWidget:
    """A markdown text widget."""

    layout: WidgetLayout
    markdown: str


@dataclass(frozen=True)
class AlarmWidget:
    """An alarm-status widget referencing one or more alarm ARNs."""

    layout: WidgetLayout
    title: str
    alarms: list[str]


@dataclass(frozen=True)
class UnknownWidget:
    """A widget whose type tail-cw does not render yet."""

    layout: WidgetLayout
    widget_type: str


Widget = MetricWidget | LogWidget | TextWidget | AlarmWidget | UnknownWidget


@dataclass(frozen=True)
class Dashboard:
    """A parsed dashboard: its name and ordered widgets."""

    name: str
    widgets: list[Widget] = field(default_factory=list)


@dataclass(frozen=True)
class DashboardSummary:
    """A dashboard listing entry."""

    name: str
    arn: str
    size: int


@dataclass(frozen=True)
class DiveCandidate:
    """A log group a widget might be backed by, with the evidence for it.

    Attributes:
        log_group: Candidate group name, conventional rather than confirmed.
        reason: Short human explanation, such as ``'SOURCE clause'``.
        exists: Whether the name was found in the account's log groups.
        event_count: Events in the widget's window, or None when the group does
            not exist and was never counted.
    """

    log_group: str
    reason: str
    exists: bool
    event_count: int | None


def _layout(widget: dict[str, Any]) -> WidgetLayout:
    return WidgetLayout(
        x=int(widget.get('x', 0)),
        y=int(widget.get('y', 0)),
        width=int(widget.get('width', _DEFAULT_WIDTH)),
        height=int(widget.get('height', _DEFAULT_HEIGHT)),
    )


def _parse_widget(widget: dict[str, Any]) -> Widget:
    layout = _layout(widget)
    widget_type = widget.get('type', 'metric')
    props = widget.get('properties', {})
    match widget_type:
        case 'metric':
            return MetricWidget(
                layout=layout,
                title=str(props.get('title', '')),
                view=str(props.get('view', 'timeSeries')),
                metrics=list(props.get('metrics', [])),
                region=props.get('region'),
                period=props.get('period'),
                stat=props.get('stat'),
                sparkline=bool(props.get('sparkline', False)),
            )
        case 'log':
            return LogWidget(
                layout=layout,
                title=str(props.get('title', '')),
                query=str(props.get('query', '')),
                view=str(props.get('view', 'table')),
                region=props.get('region'),
            )
        case 'text':
            return TextWidget(layout=layout, markdown=str(props.get('markdown', '')))
        case 'alarm':
            return AlarmWidget(
                layout=layout,
                title=str(props.get('title', '')),
                alarms=list(props.get('alarms', [])),
            )
        case _:
            return UnknownWidget(layout=layout, widget_type=str(widget_type))


def parse_dashboard_body(name: str, body: str) -> Dashboard:
    """Parse a ``DashboardBody`` JSON string into a typed Dashboard.

    Raises:
        ValueError: If the body is not valid JSON or lacks a widgets array.
    """
    try:
        data = json.loads(body)
    except json.JSONDecodeError as err:
        msg = f'Dashboard {name!r} body is not valid JSON: {err}'
        raise ValueError(msg) from err
    widgets_raw = data.get('widgets')
    if not isinstance(widgets_raw, list):
        msg = f'Dashboard {name!r} body has no widgets array'
        raise ValueError(msg)  # ruff: ignore[type-check-without-type-error]
    return Dashboard(name=name, widgets=[_parse_widget(widget) for widget in widgets_raw])


def _widget_to_dict(widget: Widget) -> dict[str, Any]:
    layout = {'x': widget.layout.x, 'y': widget.layout.y, 'width': widget.layout.width, 'height': widget.layout.height}
    match widget:
        case MetricWidget():
            return {
                'type': 'metric',
                'layout': layout,
                'title': widget.title,
                'view': widget.view,
                'period': widget.period,
                'stat': widget.stat,
                'metrics': widget.metrics,
            }
        case LogWidget():
            return {'type': 'log', 'layout': layout, 'title': widget.title, 'view': widget.view, 'query': widget.query}
        case TextWidget():
            return {'type': 'text', 'layout': layout, 'markdown': widget.markdown}
        case AlarmWidget():
            return {'type': 'alarm', 'layout': layout, 'title': widget.title, 'alarms': widget.alarms}
        case UnknownWidget():
            return {'type': widget.widget_type, 'layout': layout}


def dashboard_to_dict(dashboard: Dashboard) -> dict[str, Any]:
    """Serialize a parsed dashboard to a JSON-friendly dict for ``--json`` output."""
    return {'name': dashboard.name, 'widgets': [_widget_to_dict(widget) for widget in dashboard.widgets]}


def _row_dimensions(row: Sequence[Any]) -> dict[str, str]:
    dimensions: dict[str, str] = {}
    for index, element in enumerate(row):
        if not isinstance(element, str) or element not in _DIMENSION_NAMES:
            continue
        if index + 1 < len(row) and isinstance(row[index + 1], str):
            dimensions.setdefault(element, row[index + 1])
    return dimensions


def _metric_row_candidates(row: Sequence[Any]) -> list[tuple[str, str]]:
    dimensions = _row_dimensions(row)
    candidates: list[tuple[str, str]] = []
    if function_name := dimensions.get('FunctionName'):
        candidates.append((f'/aws/lambda/{function_name}', 'FunctionName dimension'))
    cluster = dimensions.get('ClusterName')
    service = dimensions.get('ServiceName')
    if cluster and service:
        candidates.append((f'/ecs/{cluster}/{service}', 'ClusterName and ServiceName dimensions'))
    if cluster:
        candidates.append((f'/ecs/{cluster}', 'ClusterName dimension'))
    if api_id := dimensions.get('ApiId'):
        candidates.append((f'/aws/apigateway/{api_id}', 'ApiId dimension'))
    if instance := dimensions.get('DBInstanceIdentifier'):
        candidates.append((f'/aws/rds/instance/{instance}/postgresql', 'DBInstanceIdentifier dimension'))
    if load_balancer := dimensions.get('LoadBalancer'):
        candidates.append((f'/aws/elb/{load_balancer}', 'LoadBalancer dimension'))
    return candidates


def candidate_log_groups(widget: Widget) -> list[tuple[str, str]]:
    """Rank the log groups a widget could dive into as ``(log_group, reason)``.

    Insights ``SOURCE`` clauses are exact and rank first; metric dimensions map to
    conventional group names, which may not exist. Names repeat across metric rows,
    so duplicates drop and the first rank wins.
    """
    match widget:
        case LogWidget():
            found = [(name, 'SOURCE clause') for name in _SOURCE_RE.findall(widget.query)]
        case MetricWidget():
            found = [candidate for row in widget.metrics for candidate in _metric_row_candidates(row)]
        case _:
            found = []
    reasons: dict[str, str] = {}
    for log_group, reason in found:
        reasons.setdefault(log_group, reason)
    return list(reasons.items())


def _sort_key(rank: int, candidate: DiveCandidate) -> tuple[int, int, int]:
    match candidate:
        case DiveCandidate(exists=False):
            return (2, 0, rank)
        case DiveCandidate(event_count=int() as count) if count > 0:
            return (0, -count, rank)
        case _:
            return (1, 0, rank)


def rank_dive_candidates(
    widget: Widget,
    *,
    known_groups: Collection[str],
    count_events: Callable[[str], int],
) -> list[DiveCandidate]:
    """Resolve and order dive candidates for a widget.

    Groups missing from ``known_groups`` are kept but sorted last, because the
    guessed name is still the explanation of what tail-cw looked for. Existing
    groups sort first by descending ``count_events`` result, which is called only
    for groups that exist.
    """
    candidates = [
        DiveCandidate(
            log_group=log_group,
            reason=reason,
            exists=log_group in known_groups,
            event_count=count_events(log_group) if log_group in known_groups else None,
        )
        for log_group, reason in candidate_log_groups(widget)
    ]
    ordered = sorted(enumerate(candidates), key=lambda pair: _sort_key(pair[0], pair[1]))
    return [candidate for _, candidate in ordered]


def load_dashboard_file(path: Path) -> Dashboard:
    """Load a tail-cw-native dashboard from a local JSON file.

    The file uses the same schema as a CloudWatch ``DashboardBody``, so console
    JSON can be pasted verbatim and tail-cw-specific keys are ignored by AWS.

    Raises:
        ValueError: If the file cannot be read or is not valid dashboard JSON.
    """
    try:
        body = path.read_text(encoding='utf-8')
    except OSError as err:
        msg = f'Cannot read dashboard file {path}: {err}'
        raise ValueError(msg) from err
    return parse_dashboard_body(path.stem, body)


async def list_dashboards(client: Any) -> list[DashboardSummary]:
    """List dashboards in the account, paginating fully."""
    summaries: list[DashboardSummary] = []
    paginator = client.get_paginator('list_dashboards')
    async for page in paginator.paginate():
        summaries.extend(
            DashboardSummary(
                name=entry['DashboardName'],
                arn=entry.get('DashboardArn', ''),
                size=int(entry.get('Size', 0)),
            )
            for entry in page.get('DashboardEntries', [])
        )
    return summaries


async def get_dashboard(client: Any, name: str) -> Dashboard:
    """Fetch and parse a dashboard by name."""
    response = await client.get_dashboard(DashboardName=name)
    return parse_dashboard_body(name, response['DashboardBody'])
