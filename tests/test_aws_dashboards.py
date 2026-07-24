"""Tests for dashboard body parsing into the typed widget model."""

from __future__ import annotations

import json

import pytest

from tail_cw.aws.dashboards import (
    AlarmWidget,
    LogWidget,
    MetricWidget,
    TextWidget,
    UnknownWidget,
    parse_dashboard_body,
)

_BODY = json.dumps(
    {
        'widgets': [
            {
                'type': 'metric',
                'x': 12,
                'y': 0,
                'width': 12,
                'height': 3,
                'properties': {
                    'title': 'Availability',
                    'view': 'singleValue',
                    'period': 3600,
                    'metrics': [['AWS/ApplicationELB', 'RequestCount', {'id': 'm1', 'stat': 'Sum'}]],
                },
            },
            {
                'type': 'log',
                'properties': {'title': 'Errors', 'query': "SOURCE 'x' | fields @message", 'view': 'table'},
            },
            {'type': 'text', 'properties': {'markdown': '# Uptime'}},
            {'type': 'alarm', 'properties': {'title': 'Alarms', 'alarms': ['arn:aws:cloudwatch:...:alarm/x']}},
            {'type': 'custom', 'properties': {}},
        ],
    },
)


def test_parses_each_widget_type() -> None:
    dash = parse_dashboard_body('demo', _BODY)
    assert dash.name == 'demo'
    kinds = [type(widget).__name__ for widget in dash.widgets]
    assert kinds == ['MetricWidget', 'LogWidget', 'TextWidget', 'AlarmWidget', 'UnknownWidget']


def test_metric_widget_layout_and_properties() -> None:
    metric = parse_dashboard_body('demo', _BODY).widgets[0]
    assert isinstance(metric, MetricWidget)
    assert (metric.layout.x, metric.layout.y, metric.layout.width, metric.layout.height) == (12, 0, 12, 3)
    assert metric.view == 'singleValue'
    assert metric.period == 3600
    assert metric.metrics[0][1] == 'RequestCount'


def test_log_widget_carries_query() -> None:
    log = parse_dashboard_body('demo', _BODY).widgets[1]
    assert isinstance(log, LogWidget)
    assert log.query.startswith("SOURCE 'x'")


def test_text_and_alarm_and_unknown() -> None:
    widgets = parse_dashboard_body('demo', _BODY).widgets
    assert isinstance(widgets[2], TextWidget)
    assert widgets[2].markdown == '# Uptime'
    assert isinstance(widgets[3], AlarmWidget)
    assert widgets[3].alarms == ['arn:aws:cloudwatch:...:alarm/x']
    assert isinstance(widgets[4], UnknownWidget)
    assert widgets[4].widget_type == 'custom'


def test_invalid_json_raises() -> None:
    with pytest.raises(ValueError, match='not valid JSON'):
        parse_dashboard_body('demo', '{not json')


def test_missing_widgets_array_raises() -> None:
    with pytest.raises(ValueError, match='no widgets array'):
        parse_dashboard_body('demo', json.dumps({'foo': 'bar'}))
