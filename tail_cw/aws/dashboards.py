"""CloudWatch dashboard listing, fetching, and parsing.

Reads dashboards through ``ListDashboards`` and ``GetDashboard`` and parses the
``DashboardBody`` JSON into a typed widget model shared by the console-import
path and the tail-cw-native config path. Unknown widget types are preserved as
``UnknownWidget`` so one exotic widget never fails the whole dashboard.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tail_cw.aws.client import build_client

_DEFAULT_WIDTH = 6
_DEFAULT_HEIGHT = 6


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
        raise ValueError(msg)  # noqa: TRY004
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


def list_dashboards(
    *,
    profile_name: str | None = None,
    region_name: str | None = None,
) -> list[DashboardSummary]:
    """List dashboards in the account, paginating fully."""
    client = build_client('cloudwatch', region_name=region_name, profile_name=profile_name)
    summaries: list[DashboardSummary] = []
    paginator = client.get_paginator('list_dashboards')
    for page in paginator.paginate():
        summaries.extend(
            DashboardSummary(
                name=entry['DashboardName'],
                arn=entry.get('DashboardArn', ''),
                size=int(entry.get('Size', 0)),
            )
            for entry in page.get('DashboardEntries', [])
        )
    return summaries


def get_dashboard(
    name: str,
    *,
    profile_name: str | None = None,
    region_name: str | None = None,
) -> Dashboard:
    """Fetch and parse a dashboard by name."""
    client = build_client('cloudwatch', region_name=region_name, profile_name=profile_name)
    response = client.get_dashboard(DashboardName=name)
    return parse_dashboard_body(name, response['DashboardBody'])
