"""Tests for dashboard body parsing into the typed widget model."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import pytest

from tail_cw.aws.dashboards import (
    AlarmWidget,
    LogWidget,
    MetricWidget,
    TextWidget,
    UnknownWidget,
    Widget,
    WidgetLayout,
    candidate_log_groups,
    parse_dashboard_body,
    rank_dive_candidates,
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


def _metric(*rows: Sequence[Any]) -> MetricWidget:
    return MetricWidget(layout=WidgetLayout(), title='m', view='timeSeries', metrics=[list(row) for row in rows])


def _log(query: str) -> LogWidget:
    return LogWidget(layout=WidgetLayout(), title='logs', query=query)


@pytest.mark.parametrize(
    ('row', 'expected'),
    [
        (['AWS/Lambda', 'Errors', 'FunctionName', 'my-fn'], [('/aws/lambda/my-fn', 'FunctionName dimension')]),
        (
            ['AWS/ECS', 'CPUUtilization', 'ClusterName', 'prod', 'ServiceName', 'api'],
            [
                ('/ecs/prod/api', 'ClusterName and ServiceName dimensions'),
                ('/ecs/prod', 'ClusterName dimension'),
            ],
        ),
        (['AWS/ECS', 'CPUUtilization', 'ClusterName', 'prod'], [('/ecs/prod', 'ClusterName dimension')]),
        (['AWS/ApiGateway', 'Count', 'ApiId', 'abc123'], [('/aws/apigateway/abc123', 'ApiId dimension')]),
        (
            ['AWS/RDS', 'CPUUtilization', 'DBInstanceIdentifier', 'orders'],
            [('/aws/rds/instance/orders/postgresql', 'DBInstanceIdentifier dimension')],
        ),
        (
            ['AWS/ApplicationELB', 'RequestCount', 'LoadBalancer', 'app/web/abc'],
            [('/aws/elb/app/web/abc', 'LoadBalancer dimension')],
        ),
    ],
)
def test_candidate_log_groups_maps_each_dimension(row: Sequence[Any], expected: list[tuple[str, str]]) -> None:
    assert candidate_log_groups(_metric(row)) == expected


def test_candidate_log_groups_returns_every_source_clause_in_rank_order() -> None:
    widget = _log("SOURCE '/aws/lambda/api' SOURCE '/aws/lambda/worker' | fields @message")
    assert candidate_log_groups(widget) == [
        ('/aws/lambda/api', 'SOURCE clause'),
        ('/aws/lambda/worker', 'SOURCE clause'),
    ]


def test_candidate_log_groups_deduplicates_across_metric_rows() -> None:
    row = ['AWS/Lambda', 'Errors', 'FunctionName', 'my-fn']
    other = ['AWS/Lambda', 'Invocations', 'FunctionName', 'my-fn', {'stat': 'Sum'}]
    assert candidate_log_groups(_metric(row, other)) == [('/aws/lambda/my-fn', 'FunctionName dimension')]


def test_candidate_log_groups_ignores_a_dimension_without_a_value() -> None:
    assert candidate_log_groups(_metric(['AWS/Lambda', 'Errors', 'FunctionName'])) == []


def test_candidate_log_groups_empty_for_unmapped_metric() -> None:
    assert candidate_log_groups(_metric(['AWS/SQS', 'NumberOfMessagesSent', 'QueueName', 'jobs'])) == []


def test_candidate_log_groups_empty_for_a_log_widget_without_source() -> None:
    assert candidate_log_groups(_log('fields @message | limit 20')) == []


@pytest.mark.parametrize(
    'widget',
    [
        TextWidget(layout=WidgetLayout(), markdown='# hi'),
        AlarmWidget(layout=WidgetLayout(), title='alarms', alarms=['arn:aws:cloudwatch:...:alarm/x']),
        UnknownWidget(layout=WidgetLayout(), widget_type='custom'),
    ],
)
def test_candidate_log_groups_empty_for_non_metric_widgets(widget: Widget) -> None:
    assert candidate_log_groups(widget) == []


def test_rank_dive_candidates_sorts_all_three_tiers() -> None:
    widget = _log(
        "SOURCE '/aws/lambda/silent' "
        "SOURCE '/aws/lambda/missing' "
        "SOURCE '/aws/lambda/busy' "
        "SOURCE '/aws/lambda/quiet' "
        '| fields @message',
    )
    counts = {'/aws/lambda/silent': 0, '/aws/lambda/busy': 900, '/aws/lambda/quiet': 12}
    ranked = rank_dive_candidates(widget, known_groups=set(counts), count_events=counts.__getitem__)
    assert [(c.log_group, c.exists, c.event_count) for c in ranked] == [
        ('/aws/lambda/busy', True, 900),
        ('/aws/lambda/quiet', True, 12),
        ('/aws/lambda/silent', True, 0),
        ('/aws/lambda/missing', False, None),
    ]


def test_rank_dive_candidates_never_counts_missing_groups() -> None:
    widget = _metric(['AWS/Lambda', 'Errors', 'FunctionName', 'my-fn'])
    asked: list[str] = []

    def count(log_group: str) -> int:
        asked.append(log_group)
        return 1

    ranked = rank_dive_candidates(widget, known_groups=(), count_events=count)
    assert asked == []
    assert [(c.log_group, c.reason, c.exists, c.event_count) for c in ranked] == [
        ('/aws/lambda/my-fn', 'FunctionName dimension', False, None),
    ]


def test_rank_dive_candidates_breaks_ties_on_original_rank() -> None:
    widget = _metric(['AWS/ECS', 'CPUUtilization', 'ClusterName', 'prod', 'ServiceName', 'api'])
    known = ('/ecs/prod', '/ecs/prod/api')
    ranked = rank_dive_candidates(widget, known_groups=known, count_events=lambda _: 5)
    assert [candidate.log_group for candidate in ranked] == ['/ecs/prod/api', '/ecs/prod']


def test_rank_dive_candidates_empty_for_a_text_widget() -> None:
    widget = TextWidget(layout=WidgetLayout(), markdown='# hi')
    assert rank_dive_candidates(widget, known_groups=('/aws/lambda/api',), count_events=lambda _: 1) == []
