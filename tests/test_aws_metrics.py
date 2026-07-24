"""Tests for GetMetricData shorthand translation and fetching."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from tail_cw.aws.metrics import (
    DEFAULT_STAT,
    build_metric_data_queries,
    fetch_metric_data,
)

_AVAILABILITY_SHORTHAND: list[list[Any]] = [
    [
        {
            'expression': 'IF(m_total > 0, (1 - FILL(m_5xx, 0) / m_total) * 100, 100)',
            'label': 'API Availability %',
            'id': 'e1',
        }
    ],
    [
        'AWS/ApplicationELB',
        'HTTPCode_Target_5XX_Count',
        'TargetGroup',
        'tg/abc',
        'LoadBalancer',
        'lb/xyz',
        {'visible': False, 'id': 'm_5xx', 'stat': 'Sum'},
    ],
    ['.', 'RequestCount', '.', '.', '.', '.', {'visible': False, 'id': 'm_total', 'stat': 'Sum'}],
]


def _query_by_id(queries: list[dict[str, Any]], query_id: str) -> dict[str, Any]:
    return next(query for query in queries if query['Id'] == query_id)


def test_expression_row_becomes_expression_query() -> None:
    queries = build_metric_data_queries(
        _AVAILABILITY_SHORTHAND, widget_stat=None, widget_period=3600, default_period=300
    )
    expr = _query_by_id(queries, 'e1')
    assert expr['Expression'].startswith('IF(')
    assert expr['Label'] == 'API Availability %'
    assert expr['ReturnData'] is True


def test_ditto_resolves_namespace_and_dimensions_from_previous_row() -> None:
    queries = build_metric_data_queries(
        _AVAILABILITY_SHORTHAND, widget_stat=None, widget_period=3600, default_period=300
    )
    total = _query_by_id(queries, 'm_total')['MetricStat']
    assert total['Metric']['Namespace'] == 'AWS/ApplicationELB'
    assert total['Metric']['MetricName'] == 'RequestCount'
    dims = {dim['Name']: dim['Value'] for dim in total['Metric']['Dimensions']}
    assert dims == {'TargetGroup': 'tg/abc', 'LoadBalancer': 'lb/xyz'}


def test_source_metrics_are_not_returned_data() -> None:
    queries = build_metric_data_queries(
        _AVAILABILITY_SHORTHAND, widget_stat=None, widget_period=3600, default_period=300
    )
    assert _query_by_id(queries, 'm_5xx')['ReturnData'] is False
    assert _query_by_id(queries, 'm_total')['ReturnData'] is False


def test_period_precedence_row_over_widget_over_default() -> None:
    shorthand: list[list[Any]] = [
        ['NS', 'M1', {'id': 'a', 'period': 60}],
        ['NS', 'M2', {'id': 'b'}],
    ]
    queries = build_metric_data_queries(shorthand, widget_stat='Sum', widget_period=900, default_period=300)
    assert _query_by_id(queries, 'a')['MetricStat']['Period'] == 60
    assert _query_by_id(queries, 'b')['MetricStat']['Period'] == 900


def test_stat_defaults_when_neither_row_nor_widget_set() -> None:
    queries = build_metric_data_queries(
        [['NS', 'M1', {'id': 'a'}]], widget_stat=None, widget_period=None, default_period=300
    )
    assert _query_by_id(queries, 'a')['MetricStat']['Stat'] == DEFAULT_STAT


def test_generated_ids_are_unique_and_valid_when_absent() -> None:
    shorthand: list[list[Any]] = [['NS', 'M1'], ['NS', 'M2']]
    queries = build_metric_data_queries(shorthand, widget_stat='Average', widget_period=300, default_period=300)
    ids = [query['Id'] for query in queries]
    assert len(set(ids)) == len(ids)
    assert all(query_id[0].isalpha() for query_id in ids)


def test_ellipsis_copies_whole_previous_metric() -> None:
    shorthand: list[list[Any]] = [
        ['AWS/ApplicationELB', 'TargetResponseTime', 'TargetGroup', 'tg/abc', {'stat': 'p50', 'id': 'a'}],
        ['...', {'stat': 'p90', 'id': 'b'}],
    ]
    queries = build_metric_data_queries(shorthand, widget_stat=None, widget_period=60, default_period=300)
    p90 = _query_by_id(queries, 'b')['MetricStat']
    assert p90['Metric']['Namespace'] == 'AWS/ApplicationELB'
    assert p90['Metric']['MetricName'] == 'TargetResponseTime'
    assert p90['Metric']['Dimensions'] == [{'Name': 'TargetGroup', 'Value': 'tg/abc'}]
    assert p90['Stat'] == 'p90'


def test_ellipsis_with_trailing_override_replaces_last_element() -> None:
    shorthand: list[list[Any]] = [
        ['NS', 'M1', 'Dim', 'old', {'id': 'a'}],
        ['...', 'new', {'id': 'b'}],
    ]
    queries = build_metric_data_queries(shorthand, widget_stat='Sum', widget_period=60, default_period=300)
    dims = _query_by_id(queries, 'b')['MetricStat']['Metric']['Dimensions']
    assert dims == [{'Name': 'Dim', 'Value': 'new'}]


def test_options_only_row_is_skipped() -> None:
    shorthand: list[list[Any]] = [['NS', 'M1', {'id': 'a'}], [{'label': 'orphan'}]]
    queries = build_metric_data_queries(shorthand, widget_stat='Sum', widget_period=60, default_period=300)
    assert [query['Id'] for query in queries] == ['a']


def test_ditto_without_previous_row_raises() -> None:
    with pytest.raises(ValueError, match='Ditto reference'):
        build_metric_data_queries([['.', 'M1', {'id': 'a'}]], widget_stat='Sum', widget_period=300, default_period=300)


class _FakeCloudWatch:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self._pages = pages
        self.calls: list[dict[str, Any]] = []

    def get_metric_data(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return self._pages[len(self.calls) - 1]


def test_fetch_paginates_and_concatenates_visible_series() -> None:
    ts1 = datetime(2026, 7, 24, 0, 0, tzinfo=UTC)
    ts2 = datetime(2026, 7, 24, 0, 5, tzinfo=UTC)
    pages: list[dict[str, Any]] = [
        {
            'MetricDataResults': [{'Id': 'e1', 'Label': 'Avail', 'Timestamps': [ts1], 'Values': [100.0]}],
            'NextToken': 'more',
        },
        {'MetricDataResults': [{'Id': 'e1', 'Label': 'Avail', 'Timestamps': [ts2], 'Values': [99.0]}]},
    ]
    fake = _FakeCloudWatch(pages)
    queries: list[dict[str, Any]] = [{'Id': 'e1', 'Expression': 'x', 'ReturnData': True}]
    series = fetch_metric_data(queries, ts1, ts2, client=fake)
    assert len(fake.calls) == 2
    assert fake.calls[1]['NextToken'] == 'more'
    assert len(series) == 1
    assert series[0].values == [100.0, 99.0]
    assert series[0].timestamps == [ts1, ts2]


def test_fetch_rejects_empty_queries() -> None:
    with pytest.raises(ValueError, match='At least one metric query'):
        fetch_metric_data(
            [], datetime(2026, 7, 24, tzinfo=UTC), datetime(2026, 7, 24, 1, tzinfo=UTC), client=_FakeCloudWatch([])
        )
