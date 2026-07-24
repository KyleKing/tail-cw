"""Tests for the offline demo dashboard and seed-data generators."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tail_cw.aws.dashboards import MetricWidget
from tail_cw.aws.metrics import build_metric_data_queries
from tail_cw.demo import demo_dashboard, demo_fetch_metrics, demo_resolve_logs, demo_window

_START = datetime(2026, 7, 24, 6, 0, tzinfo=UTC)
_END = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)


def test_demo_dashboard_has_a_mix_of_widget_types() -> None:
    widgets = demo_dashboard().widgets
    kinds = {type(widget).__name__ for widget in widgets}
    assert {'TextWidget', 'MetricWidget', 'LogWidget'} <= kinds


def test_demo_dashboard_metric_widgets_translate() -> None:
    for widget in demo_dashboard().widgets:
        if isinstance(widget, MetricWidget):
            queries = build_metric_data_queries(
                widget.metrics,
                widget_stat=widget.stat,
                widget_period=widget.period,
                default_period=300,
            )
            assert queries


def test_demo_fetch_returns_series_with_values() -> None:
    widget = next(w for w in demo_dashboard().widgets if isinstance(w, MetricWidget))
    queries = build_metric_data_queries(
        widget.metrics, widget_stat=widget.stat, widget_period=widget.period, default_period=300
    )
    series = demo_fetch_metrics(queries, _START, _END)
    assert series
    assert all(item.values for item in series)
    assert all(len(item.values) == len(item.timestamps) for item in series)


def test_demo_fetch_skips_hidden_source_metrics() -> None:
    queries = [
        {'Id': 'errors', 'MetricStat': {}, 'ReturnData': False},
        {'Id': 'rate', 'Expression': 'errors', 'ReturnData': True, 'Label': '5xx error rate'},
    ]
    series = demo_fetch_metrics(queries, _START, _END)
    assert [item.id for item in series] == ['rate']


def test_error_rate_series_spikes_mid_window() -> None:
    queries = [{'Id': 'rate', 'Expression': 'x', 'ReturnData': True, 'Label': '5xx error rate'}]
    values = demo_fetch_metrics(queries, _START, _END)[0].values
    assert max(values) > values[0] * 3


def test_demo_resolve_logs_writes_readable_parquet() -> None:
    path = demo_resolve_logs('demo/web-api', _START, _END)
    assert path is not None
    assert path.exists()
    assert path.stat().st_size > 0


def test_demo_window_spans_six_hours() -> None:
    start, end = demo_window(now=_END)
    assert end - start == timedelta(hours=6)
