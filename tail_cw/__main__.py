"""Entry point for the tail-cw command line interface.

Argument parsing and the fetch/tail pipelines live in ``tail_cw.cli``; this
module wires in the Textual TUI runners and handles top-level error reporting.
It can be invoked as ``python -m tail_cw``, via the ``tail-cw`` console
script, or with ``uv run tail-cw``.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from tail_cw.aws.client import LogEvent
from tail_cw.aws.live_tail import stream_live_tail
from tail_cw.aws.metrics import fetch_metric_data
from tail_cw.cli import (
    DashboardRequest,
    FetchRequest,
    TailRequest,
    iter_tail_events,
    resolve_parquet_path,
    run_cli,
)
from tail_cw.config import TailCWConfig
from tail_cw.demo import demo_fetch_metrics, demo_log_volume, demo_resolve_logs
from tail_cw.tui.app import LogTailApp
from tail_cw.tui.dashboard_app import DashboardApp

if TYPE_CHECKING:
    from tail_cw.aws.dashboards import Dashboard
    from tail_cw.aws.metrics import MetricSeries


def _run_tui(config: TailCWConfig, parquet_path: Path, request: FetchRequest) -> None:
    app = LogTailApp(config=config, parquet_path=parquet_path)

    def refetch(updated: FetchRequest) -> Path | None:
        return resolve_parquet_path(updated, config)

    app.set_fetch_context(request, refetch)
    app.run()


def _run_tail_tui(config: TailCWConfig, request: TailRequest) -> None:
    app = LogTailApp(config=config, title='CloudWatch Live Tail')

    def stream() -> Iterator[LogEvent]:
        return iter_tail_events(
            request,
            now=datetime.now(tz=UTC),
            stream_events=partial(stream_live_tail, on_sampled=app.note_live_sampled),
        )

    app.start_live_tail(stream)
    app.run()


def _run_dashboard_tui(config: TailCWConfig, dashboard: Dashboard, request: DashboardRequest) -> None:
    if request.demo:
        app = DashboardApp(
            dashboard,
            request,
            config,
            fetch_metrics=demo_fetch_metrics,
            resolve_logs=demo_resolve_logs,
            log_volume=demo_log_volume,
        )
        app.run()
        return

    def fetch_metrics(queries: Sequence[dict[str, object]], start: datetime, end: datetime) -> list[MetricSeries]:
        return fetch_metric_data(queries, start, end, profile_name=request.profile, region_name=request.region)

    def resolve_logs(log_group: str, start: datetime, end: datetime) -> Path | None:
        log_request = FetchRequest(
            log_group=log_group,
            start_time=start,
            end_time=end,
            profile=request.profile,
            region=request.region,
        )
        return resolve_parquet_path(log_request, config)

    app = DashboardApp(dashboard, request, config, fetch_metrics=fetch_metrics, resolve_logs=resolve_logs)
    app.run()


def main(argv: Sequence[str] | None = None) -> int:
    """Run the tail-cw CLI and return the process exit code."""
    try:
        return run_cli(argv, _run_tui, run_tail_tui=_run_tail_tui, run_dashboard_tui=_run_dashboard_tui)
    except KeyboardInterrupt:
        return 0
    except Exception as err:
        sys.stderr.write(f'Error: {err}\n')
        return 1


if __name__ == '__main__':
    sys.exit(main())
