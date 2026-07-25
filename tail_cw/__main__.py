"""Entry point for the tail-cw command line interface.

Argument parsing, the export pipelines, and the cache live in ``tail_cw.cli``;
this module wires the Textual shell to real AWS calls and reports top-level
errors. It can be invoked as ``python -m tail_cw``, via the ``tail-cw`` console
script, or with ``uv run tail-cw``.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import TypeVar

from tail_cw.aws.client import ClientProvider, LogEvent, client_pool, fetch_log_events
from tail_cw.aws.dashboards import Dashboard, DashboardSummary, get_dashboard, list_dashboards
from tail_cw.aws.live_tail import stream_live_tail
from tail_cw.aws.log_groups import LogGroupInfo, describe_log_groups
from tail_cw.aws.metrics import MetricSeries, fetch_metric_data
from tail_cw.cache.storage import read_parquet_to_log_events
from tail_cw.cli import FetchRequest, Session, ShellSeed, resolve_parquet_paths, run_cli
from tail_cw.concurrency import blocking_pool, run_blocking, take
from tail_cw.config import TailCWConfig
from tail_cw.demo import (
    DEMO_LOG_GROUP,
    demo_count_events,
    demo_dashboard,
    demo_fetch_metrics,
    demo_log_volume,
    demo_resolve_logs,
)
from tail_cw.preview import GroupPreview, bucket_event_counts, build_group_preview
from tail_cw.query.trace import TraceGroup, query_traces_from_parquet_files
from tail_cw.tui.navigation import NavTarget, ViewKind
from tail_cw.tui.shell import CountEvents, LoadTraces, LogVolume, ResolveLogs, ShellServices, TailCWApp
from tail_cw.tui.views import build_screen

T = TypeVar('T')

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


async def _ready(value: T) -> T:  # ruff: ignore[unused-async]
    """Present an already-computed value as an awaitable.

    Turning a value into an awaitable is the whole job here, so the missing
    ``await`` is the point rather than a blocking body in async clothing. Lets the
    demo services satisfy the awaitable ``ShellServices`` signatures.
    """
    return value


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
        load_dashboard=lambda _name: _ready(demo_dashboard()),
        list_dashboards=lambda: _ready([DashboardSummary(name='demo', arn='arn:demo', size=0)]),
        fetch_metrics=lambda queries, start, end: _ready(demo_fetch_metrics(queries, start, end)),
        log_volume=lambda group, start, end: _ready(demo_log_volume(group, start, end)),
        resolve_logs=lambda groups, start, end, pattern: _ready(_demo_resolve_logs(groups, start, end, pattern)),
        count_events=lambda group, start, end: _ready(demo_count_events(group, start, end)),
        list_groups=lambda: _ready(
            [
                LogGroupInfo(
                    name=DEMO_LOG_GROUP,
                    arn=f'arn:demo:{DEMO_LOG_GROUP}',
                    stored_bytes=None,
                    retention_days=None,
                    created=None,
                )
            ]
        ),
    )


def _cache_services(
    config: TailCWConfig,
    session: Session,
    pool: ClientProvider,
    executor: ThreadPoolExecutor,
) -> tuple[ResolveLogs, LogVolume, CountEvents, LoadTraces]:
    """Build the services that end in blocking Parquet work on ``executor``."""

    async def resolve_logs(
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
        return await resolve_parquet_paths(
            await pool.client('logs'),
            requests,
            config,
            executor=executor,
        )

    async def log_volume(log_group: str, start: datetime, end: datetime) -> list[float]:
        paths = await resolve_logs([log_group], start, end, None)
        if not paths:
            return []

        def bucket() -> list[float]:
            timestamps = (event.timestamp for event in read_parquet_to_log_events(paths[0]))
            return bucket_event_counts(timestamps, start=start, end=end)

        return await run_blocking(executor, bucket)

    async def count_events(log_group: str, start: datetime, end: datetime) -> int:
        client = await pool.client('logs')
        events = await take(fetch_log_events(client, log_group, start, end), _COUNT_EVENTS_CAP)
        return len(events)

    async def load_traces(
        paths: Sequence[Path],
        trace_id: str | None,
        trace_id_fields: Sequence[str],
        limit: int | None,
    ) -> list[TraceGroup]:
        def group() -> list[TraceGroup]:
            return query_traces_from_parquet_files(
                paths,
                trace_id=trace_id,
                trace_id_fields=list(trace_id_fields),
                limit=limit,
            )

        return await run_blocking(executor, group)

    return resolve_logs, log_volume, count_events, load_traces


def _live_services(
    config: TailCWConfig,
    session: Session,
    pool: ClientProvider,
    executor: ThreadPoolExecutor,
) -> ShellServices:
    resolve_logs, log_volume, count_events, load_traces = _cache_services(config, session, pool, executor)

    async def list_groups() -> list[LogGroupInfo]:
        logs = await pool.client('logs')
        return [group async for group in describe_log_groups(logs)]

    async def preview_group(log_group: str) -> GroupPreview:
        return await build_group_preview(
            await pool.client('logs'),
            log_group,
            window=timedelta(seconds=config.preview.window_seconds),
            now=session.end,
            config=config,
            sample_limit=config.preview.sample_limit,
            profile_name=session.profile,
            region_name=session.region,
        )

    async def load_dashboard(name: str) -> Dashboard:
        return await get_dashboard(await pool.client('cloudwatch'), name)

    async def list_account_dashboards() -> list[DashboardSummary]:
        return await list_dashboards(await pool.client('cloudwatch'))

    async def fetch_metrics(
        queries: Sequence[dict[str, object]],
        start: datetime,
        end: datetime,
    ) -> list[MetricSeries]:
        return await fetch_metric_data(await pool.client('cloudwatch'), queries, start, end)

    async def live_stream(groups: Sequence[str], filter_pattern: str | None) -> AsyncIterator[LogEvent]:
        client = await pool.client('logs')
        async for event in stream_live_tail(client, list(groups), filter_pattern=filter_pattern):
            yield event

    return ShellServices(
        list_groups=list_groups,
        preview_group=preview_group,
        list_dashboards=list_account_dashboards,
        load_dashboard=load_dashboard,
        fetch_metrics=fetch_metrics,
        resolve_logs=resolve_logs,
        live_stream=live_stream,
        log_volume=log_volume,
        count_events=count_events,
        load_traces=load_traces,
    )


def _build_app(config: TailCWConfig, session: Session, seed: ShellSeed, services: ShellServices) -> TailCWApp:
    return TailCWApp(
        config,
        session,
        build_screen=build_screen,
        services=services,
        target=_seed_to_target(seed),
    )


async def _run_shell_async(config: TailCWConfig, session: Session, seed: ShellSeed) -> None:
    if seed.demo:
        await _build_app(config, session, seed, _demo_services()).run_async()
        return
    with blocking_pool() as executor:
        async with client_pool(profile_name=session.profile, region_name=session.region) as pool:
            services = _live_services(config, session, pool, executor)
            await _build_app(config, session, seed, services).run_async()


def _run_shell(config: TailCWConfig, session: Session, seed: ShellSeed) -> None:
    asyncio.run(_run_shell_async(config, session, seed))


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
