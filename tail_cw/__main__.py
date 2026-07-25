"""Entry point for the tail-cw command line interface.

Argument parsing, the export pipelines, and the cache live in ``tail_cw.cli``;
this module wires the Textual shell to real AWS calls and reports top-level
errors. It can be invoked as ``python -m tail_cw``, via the ``tail-cw`` console
script, or with ``uv run tail-cw``.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator, Sequence
from datetime import datetime, timedelta
from itertools import islice
from pathlib import Path

from tail_cw.aws.client import LogEvent, fetch_log_events
from tail_cw.aws.dashboards import Dashboard, DashboardSummary, get_dashboard, list_dashboards
from tail_cw.aws.live_tail import stream_live_tail
from tail_cw.aws.log_groups import LogGroupInfo, describe_log_groups
from tail_cw.aws.metrics import MetricSeries, fetch_metric_data
from tail_cw.cli import FetchRequest, Session, ShellSeed, resolve_parquet_paths, run_cli
from tail_cw.config import TailCWConfig
from tail_cw.demo import demo_dashboard, demo_fetch_metrics, demo_log_volume, demo_resolve_logs
from tail_cw.preview import GroupPreview, build_group_preview
from tail_cw.tui.navigation import NavTarget, ViewKind
from tail_cw.tui.shell import ShellServices, TailCWApp
from tail_cw.tui.views import build_screen

_COUNT_EVENTS_CAP = 1000


def _seed_to_target(seed: ShellSeed) -> NavTarget:
    match seed.view:
        case 'logs':
            return NavTarget(kind=ViewKind.LOGS, label=f'logs {_target_label(seed)}', payload=seed.targets)
        case 'tail':
            return NavTarget(kind=ViewKind.LOGS, label=f'tail {_target_label(seed)}', payload=seed.targets)
        case 'dashboards':
            return NavTarget(kind=ViewKind.DASHBOARDS, label='dashboards')
        case 'dashboard':
            name = seed.targets[0] if seed.targets else 'dashboard'
            return NavTarget(kind=ViewKind.DASHBOARD, label=name, payload=(name,))
        case _:
            return NavTarget(kind=ViewKind.GROUPS, label='groups')


def _target_label(seed: ShellSeed) -> str:
    if len(seed.targets) == 1:
        return seed.targets[0]
    return f'{len(seed.targets)} groups'


def _demo_resolve_logs(
    groups: Sequence[str],
    start: datetime,
    end: datetime,
    _filter_pattern: str | None,
) -> list[Path]:
    paths = (demo_resolve_logs(group, start, end) for group in groups or ('demo',))
    return [path for path in paths if path is not None]


def _demo_services() -> ShellServices:
    return ShellServices(
        load_dashboard=lambda _name: demo_dashboard(),
        list_dashboards=lambda: [DashboardSummary(name='demo', arn='arn:demo', size=0)],
        fetch_metrics=demo_fetch_metrics,
        log_volume=demo_log_volume,
        resolve_logs=_demo_resolve_logs,
        count_events=lambda _group, _start, _end: 1,
        list_groups=list,
    )


def _live_services(config: TailCWConfig, session: Session) -> ShellServices:
    def list_groups() -> list[LogGroupInfo]:
        return list(describe_log_groups(profile_name=session.profile, region_name=session.region))

    def preview_group(log_group: str) -> GroupPreview:
        return build_group_preview(
            log_group,
            window=timedelta(seconds=config.preview.window_seconds),
            now=session.end,
            config=config,
            profile_name=session.profile,
            region_name=session.region,
        )

    def load_dashboard(name: str) -> Dashboard:
        return get_dashboard(name, profile_name=session.profile, region_name=session.region)

    def fetch_metrics(
        queries: Sequence[dict[str, object]],
        start: datetime,
        end: datetime,
    ) -> list[MetricSeries]:
        return fetch_metric_data(queries, start, end, profile_name=session.profile, region_name=session.region)

    def resolve_logs(
        groups: Sequence[str],
        start: datetime,
        end: datetime,
        filter_pattern: str | None,
    ) -> list[Path]:
        requests = [
            FetchRequest(
                log_group=group,
                start_time=start,
                end_time=end,
                filter_pattern=filter_pattern,
                profile=session.profile,
                region=session.region,
            )
            for group in groups
        ]
        return resolve_parquet_paths(requests, config)

    def live_stream(groups: Sequence[str], filter_pattern: str | None) -> Iterator[LogEvent]:
        return stream_live_tail(
            list(groups),
            filter_pattern=filter_pattern,
            profile_name=session.profile,
            region_name=session.region,
        )

    def count_events(log_group: str, start: datetime, end: datetime) -> int:
        events = fetch_log_events(
            log_group,
            start,
            end,
            profile_name=session.profile,
            region_name=session.region,
        )
        return sum(1 for _ in islice(events, _COUNT_EVENTS_CAP))

    return ShellServices(
        list_groups=list_groups,
        preview_group=preview_group,
        list_dashboards=lambda: list_dashboards(profile_name=session.profile, region_name=session.region),
        load_dashboard=load_dashboard,
        fetch_metrics=fetch_metrics,
        resolve_logs=resolve_logs,
        live_stream=live_stream,
        count_events=count_events,
    )


def _run_shell(config: TailCWConfig, session: Session, seed: ShellSeed) -> None:
    services = _demo_services() if seed.demo else _live_services(config, session)
    app = TailCWApp(
        config,
        session,
        build_screen=build_screen,
        services=services,
        target=_seed_to_target(seed),
    )
    app.run()


def main(argv: Sequence[str] | None = None) -> int:
    """Run the tail-cw CLI and return the process exit code."""
    try:
        return run_cli(argv, _run_shell)
    except KeyboardInterrupt:
        return 0
    except Exception as err:
        sys.stderr.write(f'Error: {err}\n')
        return 1


if __name__ == '__main__':
    sys.exit(main())
