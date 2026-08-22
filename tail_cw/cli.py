"""Command line interface for tail-cw.

Provides the argparse subcommand parser, time parsing helpers, and the fetch
pipeline that connects the AWS client, the Parquet cache, and the output
surfaces (NDJSON to stdout or the Textual TUI). This module intentionally does
not import Textual; the TUI runner is injected by ``tail_cw.__main__``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import chain
from pathlib import Path
from typing import Any, Literal

from beartype.typing import Protocol

from tail_cw.aws.alarms import ALARM_STATES, AlarmSummary, describe_alarm_history, describe_alarms
from tail_cw.aws.client import ClientProvider, LogEvent, client_pool, fetch_log_events
from tail_cw.aws.dashboards import (
    DashboardSummary,
    dashboard_to_dict,
    get_dashboard,
    list_dashboards,
    load_dashboard_file,
)
from tail_cw.aws.insights import (
    MAX_INSIGHTS_LOG_GROUPS,
    InsightsQueryError,
    InsightsResult,
    estimate_scan,
    run_insights_query,
    validate_insights_request,
)
from tail_cw.aws.live_tail import MAX_LIVE_TAIL_LOG_GROUPS, stream_live_tail
from tail_cw.aws.log_groups import LogGroupInfo, describe_log_groups, resolve_group_pattern
from tail_cw.aws.metrics import (
    DEFAULT_PERIOD_SECONDS,
    MetricSeries,
    build_metric_data_queries,
    fetch_metric_data,
)
from tail_cw.cache.storage import LogCache, generate_cache_key
from tail_cw.cache.window import Segment, plan_segments
from tail_cw.concurrency import blocking_pool, closing_stream, consume_in_thread, run_blocking
from tail_cw.config import TailCWConfig, get_default_cache_dir, load_config
from tail_cw.demo import demo_dashboard
from tail_cw.history import HistoryKind, append, make_entry
from tail_cw.query.engine import query_parquet_files_to_log_events
from tail_cw.query.fuzzy import DEFAULT_SIMILARITY
from tail_cw.query.parser import FilterNode, parse_filter_pattern
from tail_cw.query.report import render_alarm_markdown, render_markdown, render_rows_markdown
from tail_cw.query.rollup import DEFAULT_PATTERN_LIMIT, Granularity, RollupReport, roll_up
from tail_cw.query.severity import Severity

FetchEvents = Callable[..., AsyncIterator[LogEvent]]
StreamEvents = Callable[..., AsyncIterator[LogEvent]]
ShellView = Literal['groups', 'logs', 'tail', 'dashboards', 'dashboard']

DEFAULT_WINDOW = '1h'
DEFAULT_DASHBOARD_WINDOW = '3h'
DEFAULT_SUMMARY_MAX_GROUPS = 25
INSIGHTS_DEFAULT_LIMIT = 1000
DEFAULT_HISTORY_WINDOW = '7d'

_DURATION_RE = re.compile(r'(\d+)([dhm])')


class SupportsWriteStr(Protocol):
    """Text sink accepting str writes (e.g. sys.stdout, io.StringIO)."""

    def write(self, text: str, /) -> int:
        """Write text and return the number of characters written."""
        raise NotImplementedError


class SupportsWriteFlushStr(SupportsWriteStr, Protocol):
    """Text sink that can also flush buffered writes (e.g. sys.stdout)."""

    def flush(self) -> None:
        """Flush buffered writes."""
        raise NotImplementedError


@dataclass(frozen=True)
class FetchRequest:
    """Resolved parameters for a CloudWatch fetch.

    Attributes:
        log_group: CloudWatch log group name.
        start_time: Start of the time range (timezone-aware).
        end_time: End of the time range (timezone-aware).
        profile: Optional AWS profile name.
        region: Optional AWS region name.

    No filter belongs here: a historical fetch retrieves the whole window so one
    cached copy serves every filter, which is then applied locally on read.
    """

    log_group: str
    start_time: datetime
    end_time: datetime
    profile: str | None = None
    region: str | None = None


@dataclass(frozen=True)
class TailRequest:
    """Resolved parameters for a live tail session.

    Attributes:
        log_groups: One to ten CloudWatch log group names.
        filter_pattern: Optional filter applied server-side to both the live
            stream (``logEventFilterPattern``) and the backfill fetch.
        backfill_start: When set, historical events from this time to session
            start are emitted before switching to the live stream.
        profile: Optional AWS profile name.
        region: Optional AWS region name.
    """

    log_groups: tuple[str, ...]
    filter_pattern: str | None = None
    backfill_start: datetime | None = None
    profile: str | None = None
    region: str | None = None


@dataclass(frozen=True)
class DashboardRequest:
    """Resolved parameters for opening a dashboard.

    Attributes:
        name: Dashboard name, or the local file stem when loaded from a file.
        start_time: Start of the metric window (timezone-aware).
        end_time: End of the metric window (timezone-aware).
        profile: Optional AWS profile name.
        region: Optional AWS region name.
    """

    name: str
    start_time: datetime
    end_time: datetime
    profile: str | None = None
    region: str | None = None
    demo: bool = False


@dataclass(slots=True)
class Session:
    """State the interactive shell shares across every view.

    The window and filter follow the user between views on purpose: a range
    set on a dashboard is the range a dive inherits. Lives here rather than in
    the TUI so the CLI can build it without importing Textual.
    """

    start: datetime
    end: datetime
    filter_pattern: str | None = None
    profile: str | None = None
    region: str | None = None
    selected_groups: list[str] = field(default_factory=list)
    dashboard_names: list[str] = field(default_factory=list)
    group_names: list[str] = field(default_factory=list)

    def window_label(self) -> str:
        """Render the window as a compact status-line fragment."""
        return f'{self.start:%Y-%m-%d %H:%M}->{self.end:%H:%M} UTC'


@dataclass(frozen=True)
class ShellSeed:
    """Which view the shell opens on, and what it opens there.

    ``targets`` holds log group patterns for the log views and a dashboard
    name for the dashboard view. The entry point translates this into a
    navigation target, keeping this module free of any TUI import.
    """

    view: ShellView
    targets: tuple[str, ...] = ()
    demo: bool = False


def _duration_to_timedelta(amount: int, unit: str) -> timedelta:
    match unit:
        case 'd':
            return timedelta(days=amount)
        case 'h':
            return timedelta(hours=amount)
        case 'm':
            return timedelta(minutes=amount)
        case _:
            msg = f'Unsupported duration unit: {unit!r}'
            raise ValueError(msg)


def parse_time(value: str, *, now: datetime) -> datetime:
    """Parse a relative duration (``15m``, ``2h``, ``3d``) or ISO-8601 datetime.

    Relative durations are interpreted as offsets before ``now``. Naive
    absolute datetimes are assumed to be UTC.

    Raises:
        ValueError: If the value is neither a supported duration nor a valid
            ISO-8601 datetime.
    """
    text = value.strip()
    if match := _DURATION_RE.fullmatch(text):
        return now - _duration_to_timedelta(int(match.group(1)), match.group(2))
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as err:
        msg = f'Invalid time {value!r}: expected a duration like 15m, 2h, or 3d, or an ISO-8601 datetime'
        raise ValueError(msg) from err
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _add_aws_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument('--config', dest='config_path', type=Path, default=None, help='Config file path override')
    parser.add_argument('--profile', default=None, help='AWS profile name')
    parser.add_argument('--region', default=None, help='AWS region name')


def _add_window_flags(parser: argparse.ArgumentParser, *, default_start: str) -> None:
    parser.add_argument(
        '--start',
        default=default_start,
        help=f'Start of range: duration (15m, 2h, 3d) or ISO-8601 datetime (default: {default_start})',
    )
    parser.add_argument('--end', default=None, help='End of range: duration (2h) or ISO-8601 datetime (default: now)')
    parser.add_argument('--filter', dest='filter_pattern', default=None, help='CloudWatch Logs filter pattern')


def _add_export_parsers(export: argparse.ArgumentParser) -> None:
    """Attach the ``export`` subcommand tree, which owns most of the CLI surface."""
    export_sub = export.add_subparsers(dest='export_command')
    _configure_logs(export_sub.add_parser('logs', help='Write log events for a time range as NDJSON.'))
    _configure_tail(export_sub.add_parser('tail', help='Stream live log events as NDJSON (Ctrl+C to stop).'))
    _configure_groups(export_sub.add_parser('groups', help='Write log group metadata as NDJSON.'))
    _configure_summary(
        export_sub.add_parser('summary', help='Roll matching log groups up into recurring error and warning patterns.')
    )
    _configure_insights(
        export_sub.add_parser('insights', help='Run a CloudWatch Logs Insights query (billed per GB scanned).')
    )
    _configure_alarms(export_sub.add_parser('alarms', help='Write metric alarms, and their firing history, as NDJSON.'))
    _configure_metrics(export_sub.add_parser('metrics', help='Write metric datapoints as NDJSON.'))
    _configure_dashboards(export_sub.add_parser('dashboards', help='Write the account dashboard list as NDJSON.'))
    _configure_dashboard(export_sub.add_parser('dashboard', help='Write one parsed dashboard structure as JSON.'))


def _configure_logs(parser: argparse.ArgumentParser) -> None:
    parser.add_argument('log_group', help='CloudWatch log group name (e.g. /aws/lambda/my-function)')
    _add_aws_flags(parser)
    _add_window_flags(parser, default_start=DEFAULT_WINDOW)
    parser.add_argument(
        '--no-cache',
        dest='no_cache',
        action='store_true',
        help='Bypass the cache read (results are still written to the cache)',
    )


def _configure_tail(parser: argparse.ArgumentParser) -> None:
    parser.add_argument('log_groups', nargs='+', help='One or more CloudWatch log group names (max 10)')
    _add_aws_flags(parser)
    parser.add_argument('--filter', dest='filter_pattern', default=None, help='CloudWatch Logs filter pattern')
    parser.add_argument(
        '--backfill',
        default=None,
        help='Emit historical events for this window (e.g. 15m) before streaming live',
    )


def _configure_groups(parser: argparse.ArgumentParser) -> None:
    parser.add_argument('pattern', nargs='?', default=None, help='Name, prefix, or glob to match')
    _add_aws_flags(parser)


def _configure_summary(parser: argparse.ArgumentParser) -> None:
    parser.add_argument('patterns', nargs='*', help='Log group names or glob patterns (omit for every group)')
    _add_aws_flags(parser)
    _add_window_flags(parser, default_start=DEFAULT_WINDOW)
    parser.add_argument(
        '--level',
        choices=[level.name.lower() for level in Severity],
        default=Severity.WARNING.name.lower(),
        help='Minimum severity to include (default: warning)',
    )
    parser.add_argument(
        '--by',
        dest='granularity',
        choices=[value.value for value in Granularity],
        default=Granularity.HOUR.value,
        help='Time bucket for the per-period counts (default: hour)',
    )
    parser.add_argument(
        '--top',
        type=int,
        default=DEFAULT_PATTERN_LIMIT,
        help=f'Number of patterns to report (default: {DEFAULT_PATTERN_LIMIT})',
    )
    parser.add_argument(
        '--format',
        dest='output_format',
        choices=['md', 'json'],
        default='md',
        help='Markdown document or one JSON object (default: md)',
    )
    parser.add_argument(
        '--max-groups',
        type=int,
        default=DEFAULT_SUMMARY_MAX_GROUPS,
        help=f'Cap on groups fetched; the rest are named on stderr (default: {DEFAULT_SUMMARY_MAX_GROUPS})',
    )
    parser.add_argument(
        '--similarity',
        type=float,
        default=DEFAULT_SIMILARITY,
        help=f'Fuzzy merge threshold for near-identical shapes, 0 to disable (default: {DEFAULT_SIMILARITY})',
    )
    parser.add_argument('--no-cache', action='store_true', help='Bypass the cache read')


def _configure_insights(parser: argparse.ArgumentParser) -> None:
    parser.add_argument('patterns', nargs='*', help='Log group names or glob patterns')
    _add_aws_flags(parser)
    parser.add_argument(
        '--start',
        default=DEFAULT_WINDOW,
        help=f'Start of range: duration (15m, 2h, 3d) or ISO-8601 datetime (default: {DEFAULT_WINDOW})',
    )
    parser.add_argument('--end', default=None, help='End of range: duration or ISO-8601 datetime')
    parser.add_argument('--query', required=True, help='Logs Insights query string')
    parser.add_argument(
        '--limit',
        type=int,
        default=INSIGHTS_DEFAULT_LIMIT,
        help=f'Maximum rows returned (default: {INSIGHTS_DEFAULT_LIMIT})',
    )
    parser.add_argument(
        '--format',
        dest='output_format',
        choices=['ndjson', 'md'],
        default='ndjson',
        help='One JSON object per row, or a markdown table (default: ndjson)',
    )
    parser.add_argument(
        '--max-groups',
        type=int,
        default=MAX_INSIGHTS_LOG_GROUPS,
        help=f'Cap on groups queried (default: {MAX_INSIGHTS_LOG_GROUPS}, the Insights maximum)',
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Print the scan estimate and stop without querying',
    )
    parser.add_argument(
        '--yes',
        action='store_true',
        help='Run even when the estimate is above [insights].confirm_above_gb',
    )


def _configure_alarms(parser: argparse.ArgumentParser) -> None:
    parser.add_argument('prefix', nargs='?', default=None, help='Restrict to alarms whose name starts with this')
    _add_aws_flags(parser)
    parser.add_argument(
        '--state',
        action='append',
        choices=list(ALARM_STATES),
        default=None,
        help='Restrict to a state; repeatable (default: every state)',
    )
    parser.add_argument(
        '--history',
        action='store_true',
        help="Also count and list each alarm's state transitions in the window",
    )
    parser.add_argument(
        '--start',
        default=DEFAULT_HISTORY_WINDOW,
        help=f'Start of the history window (default: {DEFAULT_HISTORY_WINDOW})',
    )
    parser.add_argument('--end', default=None, help='End of the history window')


def _configure_metrics(parser: argparse.ArgumentParser) -> None:
    parser.add_argument('--namespace', required=True, help='Metric namespace, e.g. AWS/ECS')
    parser.add_argument('--metric', required=True, help='Metric name, e.g. MemoryUtilization')
    parser.add_argument(
        '--dimension',
        action='append',
        default=None,
        metavar='NAME=VALUE',
        help='Dimension filter; repeatable',
    )
    parser.add_argument('--stat', default='Average', help='Statistic, e.g. Average, Maximum, p99')
    parser.add_argument('--period', type=int, default=None, help='Period in seconds (default: from config)')
    _add_aws_flags(parser)
    parser.add_argument(
        '--start',
        default=DEFAULT_DASHBOARD_WINDOW,
        help=f'Start of range: duration or ISO-8601 datetime (default: {DEFAULT_DASHBOARD_WINDOW})',
    )
    parser.add_argument('--end', default=None, help='End of range: duration or ISO-8601 datetime')


def _configure_dashboards(parser: argparse.ArgumentParser) -> None:
    _add_aws_flags(parser)


def _configure_dashboard(parser: argparse.ArgumentParser) -> None:
    parser.add_argument('name', nargs='?', default=None, help='Dashboard name (omit with --file or --demo)')
    _add_aws_flags(parser)
    parser.add_argument(
        '--demo',
        dest='demo',
        action='store_true',
        help='Emit the synthetic demo dashboard (no AWS calls)',
    )
    parser.add_argument(
        '--file',
        dest='dashboard_file',
        type=Path,
        default=None,
        help='Load a local dashboard JSON file (same schema as a CloudWatch DashboardBody)',
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the tail-cw argument parser.

    Bare ``tail-cw`` opens the interactive shell. ``logs``, ``tail``, and
    ``dash`` open it on a specific view; ``export`` is the only subcommand that
    writes to stdout instead.
    """
    parser = argparse.ArgumentParser(
        prog='tail-cw',
        description='Read and explore AWS CloudWatch from the terminal. Run with no arguments to browse log groups.',
    )
    _add_aws_flags(parser)
    subparsers = parser.add_subparsers(dest='command')

    logs = subparsers.add_parser('logs', help='Open the log view on the groups matching a pattern.')
    logs.add_argument('patterns', nargs='*', help='Log group names or glob patterns (omit to use the browser)')
    _add_aws_flags(logs)
    _add_window_flags(logs, default_start=DEFAULT_WINDOW)
    logs.add_argument(
        '--no-cache',
        dest='no_cache',
        action='store_true',
        help='Bypass the cache read (results are still written to the cache)',
    )

    tail = subparsers.add_parser('tail', help='Open the log view streaming live events.')
    tail.add_argument('patterns', nargs='*', help='Log group names or glob patterns (max 10)')
    _add_aws_flags(tail)
    _add_window_flags(tail, default_start=DEFAULT_WINDOW)

    dash = subparsers.add_parser('dash', help='Open a dashboard, or the dashboard picker when unnamed.')
    dash.add_argument('name', nargs='?', default=None, help='Dashboard name (omit to pick from a list)')
    _add_aws_flags(dash)
    _add_window_flags(dash, default_start=DEFAULT_DASHBOARD_WINDOW)
    dash.add_argument(
        '--demo',
        dest='demo',
        action='store_true',
        help='Open a synthetic dashboard with generated seed data (no AWS calls)',
    )

    export = subparsers.add_parser('export', help='Write CloudWatch data to stdout as NDJSON or JSON.')
    _add_export_parsers(export)

    return parser


async def iter_tail_events(
    client: Any,
    request: TailRequest,
    *,
    now: datetime,
    fetch_events: FetchEvents | None = None,
    stream_events: StreamEvents | None = None,
) -> AsyncIterator[LogEvent]:
    """Yield backfill events (when requested) followed by the live stream.

    The same filter pattern is pushed server-side to both FilterLogEvents
    (backfill) and StartLiveTail (``logEventFilterPattern``). The live path
    does not touch the Parquet cache.
    """
    effective_fetch = fetch_events if fetch_events is not None else fetch_log_events
    effective_stream = stream_events if stream_events is not None else stream_live_tail
    if request.backfill_start is not None:
        for log_group in request.log_groups:
            async for event in effective_fetch(
                client,
                log_group,
                request.backfill_start,
                now,
                filter_pattern=request.filter_pattern,
            ):
                yield event
    async for event in effective_stream(client, request.log_groups, filter_pattern=request.filter_pattern):
        yield event


def _local_filter(filter_pattern: str | None) -> FilterNode | None:
    """Parse a ``--filter`` value for local evaluation against cached events."""
    return parse_filter_pattern(filter_pattern) if filter_pattern else None


def open_log_cache(config: TailCWConfig) -> LogCache:
    """Open the configured log cache. Close it, or use it as a context manager."""
    return LogCache(
        request_cache_dir(config),
        size_limit_mb=config.cache.size_limit_mb,
        default_ttl_seconds=config.cache.default_ttl_seconds,
        eviction_policy=config.cache.eviction_policy,
    )


async def _resolve_segment(
    client: Any,
    request: FetchRequest,
    segment: Segment,
    cache: LogCache,
    *,
    use_cache: bool,
    fetch_events: FetchEvents,
    executor: ThreadPoolExecutor | None,
) -> Path | None:
    cache_key = generate_cache_key(
        request.log_group,
        segment.start,
        segment.end,
        region_name=request.region,
        profile_name=request.profile,
    )
    # An unsettled segment is short of events CloudWatch had not ingested yet, so a
    # hit on it is refetched rather than served.
    if use_cache and segment.settled and (cached_path := cache.get_parquet_path(cache_key)) is not None:
        return cached_path
    events = fetch_events(client, request.log_group, segment.start, segment.end)
    first_event = await anext(events, None)
    if first_event is None:
        return None

    def write(remaining: Iterator[LogEvent]) -> Path | None:
        cache.write(chain([first_event], remaining), cache_key, ttl_seconds=segment.ttl_seconds)
        return cache.get_parquet_path(cache_key)

    return await consume_in_thread(executor, events, write)


async def _resolve_into_cache(
    client: Any,
    request: FetchRequest,
    cache: LogCache,
    *,
    now: datetime,
    use_cache: bool,
    fetch_events: FetchEvents | None,
    executor: ThreadPoolExecutor | None,
) -> list[Path]:
    effective_fetch = fetch_events if fetch_events is not None else fetch_log_events
    paths = []
    # Segments run one at a time. FilterLogEvents is quota-limited per account and
    # the fan-out across log groups already saturates it.
    for segment in plan_segments(request.start_time, request.end_time, now=now):
        path = await _resolve_segment(
            client,
            request,
            segment,
            cache,
            use_cache=use_cache,
            fetch_events=effective_fetch,
            executor=executor,
        )
        if path is not None and path not in paths:
            paths.append(path)
    return paths


async def resolve_parquet_path(
    client: Any,
    request: FetchRequest,
    config: TailCWConfig,
    *,
    now: datetime | None = None,
    use_cache: bool = True,
    fetch_events: FetchEvents | None = None,
    executor: ThreadPoolExecutor | None = None,
) -> list[Path]:
    """Return the cached Parquet segments for one request, fetching on miss.

    Returns an empty list when the request matches no events.
    """
    with open_log_cache(config) as cache:
        return await _resolve_into_cache(
            client,
            request,
            cache,
            now=now if now is not None else datetime.now(UTC),
            use_cache=use_cache,
            fetch_events=fetch_events,
            executor=executor,
        )


async def resolve_parquet_paths(
    client: Any,
    requests: Sequence[FetchRequest],
    config: TailCWConfig,
    *,
    now: datetime | None = None,
    use_cache: bool = True,
    fetch_events: FetchEvents | None = None,
    executor: ThreadPoolExecutor | None = None,
) -> list[Path]:
    """Resolve several fetches concurrently, dropping the ones with no events.

    Each request becomes one file per aligned segment of its window, so the
    interior of a relative window is reusable by the next command. The caller
    merges the files at read time.

    Every request shares one ``LogCache``. Separate instances over the same
    directory delete each other's not-yet-referenced Parquet files during orphan
    cleanup, which silently drops groups from the result.

    Fetches all start together; the Parquet writes queue on ``executor``, which is
    where the real bound sits. A failure cancels the siblings rather than leaving
    them to finish writing into a cache nobody will read.
    """
    if not requests:
        return []
    resolved_now = now if now is not None else datetime.now(UTC)
    with open_log_cache(config) as cache:

        async def resolve(request: FetchRequest) -> list[Path]:
            return await _resolve_into_cache(
                client,
                request,
                cache,
                now=resolved_now,
                use_cache=use_cache,
                fetch_events=fetch_events,
                executor=executor,
            )

        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(resolve(request)) for request in requests]
    return [path for task in tasks for path in task.result()]


def request_cache_dir(config: TailCWConfig) -> Path:
    """Return the configured cache directory, falling back to the XDG default."""
    return config.cache.cache_dir if config.cache.cache_dir is not None else get_default_cache_dir()


def _event_to_record(event: LogEvent) -> dict[str, str]:
    return {
        'timestamp': event.timestamp.isoformat(),
        'log_group': event.log_group,
        'log_stream': event.log_stream,
        'message': event.message,
    }


def write_ndjson(events: Iterable[LogEvent], stream: SupportsWriteStr) -> int:
    """Write log events to a stream as NDJSON and return the number written."""
    count = 0
    for event in events:
        stream.write(json.dumps(_event_to_record(event), separators=(',', ':')) + '\n')
        count += 1
    return count


async def stream_ndjson(events: AsyncIterator[LogEvent], stream: SupportsWriteFlushStr) -> int:
    """Write log events as NDJSON, flushing after every line for live consumers."""
    count = 0
    async for event in events:
        stream.write(json.dumps(_event_to_record(event), separators=(',', ':')) + '\n')
        stream.flush()
        count += 1
    return count


RunShell = Callable[[TailCWConfig, Session, ShellSeed], None]


def _dashboard_summary_to_record(summary: DashboardSummary) -> dict[str, object]:
    return {'name': summary.name, 'arn': summary.arn, 'size': summary.size}


def _log_group_to_record(group: LogGroupInfo) -> dict[str, object]:
    return {
        'name': group.name,
        'arn': group.arn,
        'stored_bytes': group.stored_bytes,
        'retention_days': group.retention_days,
        'created': group.created.isoformat() if group.created is not None else None,
    }


def _write_json_line(record: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(record, separators=(',', ':')) + '\n')


def _load_config_or_report(config_path: Path | None) -> TailCWConfig | None:
    try:
        return load_config(config_path)
    except (OSError, ValueError) as err:
        sys.stderr.write(f'Configuration error: {err}\n')
        return None


def _window_from_args(args: argparse.Namespace, now: datetime) -> tuple[datetime, datetime]:
    start_time = parse_time(args.start, now=now)
    end_time = parse_time(args.end, now=now) if args.end is not None else now
    if start_time >= end_time:
        msg = f'--start ({start_time.isoformat()}) must be before --end ({end_time.isoformat()})'
        raise ValueError(msg)
    return start_time, end_time


def session_from_args(args: argparse.Namespace, now: datetime) -> Session:
    """Build the shared shell session from parsed arguments."""
    start = getattr(args, 'start', DEFAULT_WINDOW)
    end = getattr(args, 'end', None)
    start_time = parse_time(start, now=now)
    end_time = parse_time(end, now=now) if end is not None else now
    if start_time >= end_time:
        msg = f'--start ({start_time.isoformat()}) must be before --end ({end_time.isoformat()})'
        raise ValueError(msg)
    return Session(
        start=start_time,
        end=end_time,
        filter_pattern=getattr(args, 'filter_pattern', None),
        profile=args.profile,
        region=args.region,
    )


def expand_presets(patterns: Sequence[str], presets: Mapping[str, Sequence[str]]) -> list[str]:
    """Replace every ``@name`` reference with the log groups of that named preset.

    Patterns that are not references pass through untouched. Shared by the CLI
    seeds and the shell's ``:logs``/``:tail`` so the two cannot drift.

    Raises:
        ValueError: When a reference names no configured preset, or names one
            that holds no log groups. Expanding to nothing silently would look
            like an empty selection.
    """
    expanded: list[str] = []
    for pattern in patterns:
        if not pattern.startswith('@'):
            expanded.append(pattern)
            continue
        name = pattern.removeprefix('@')
        groups = presets.get(name)
        if groups is None:
            known = ', '.join(f'@{key}' for key in sorted(presets)) or 'none configured'
            msg = f'Unknown preset {pattern!r}; configured presets: {known}'
            raise ValueError(msg)
        if not groups:
            msg = f'Preset {pattern!r} lists no log groups'
            raise ValueError(msg)
        expanded.extend(groups)
    return expanded


def _log_view_seed(
    view: Literal['logs', 'tail'],
    patterns: Sequence[str],
    presets: Mapping[str, Sequence[str]],
) -> ShellSeed:
    expanded = expand_presets(patterns, presets)
    return ShellSeed(view=view if expanded else 'groups', targets=tuple(expanded))


def seed_from_args(args: argparse.Namespace, presets: Mapping[str, Sequence[str]] | None = None) -> ShellSeed:
    """Choose the opening view from the subcommand and its arguments.

    A log group argument naming an unknown preset propagates ``ValueError`` from
    :func:`expand_presets`.
    """
    known = presets if presets is not None else {}
    match args.command:
        case 'logs':
            return _log_view_seed('logs', args.patterns, known)
        case 'tail':
            return _log_view_seed('tail', args.patterns, known)
        case 'dash' if args.demo:
            return ShellSeed(view='dashboard', targets=('demo',), demo=True)
        case 'dash' if args.name is not None:
            return ShellSeed(view='dashboard', targets=(args.name,))
        case 'dash':
            return ShellSeed(view='dashboards')
        case _:
            return ShellSeed(view='groups')


def _run_shell_command(args: argparse.Namespace, now: datetime, run_shell: RunShell | None) -> int:
    if run_shell is None:
        sys.stderr.write('The interactive shell is unavailable in this entry point; use tail-cw export\n')
        return 1
    try:
        session = session_from_args(args, now)
    except ValueError as err:
        sys.stderr.write(f'{err}\n')
        return 2
    config = _load_config_or_report(args.config_path)
    if config is None:
        return 1
    try:
        seed = seed_from_args(args, config.presets)
    except ValueError as err:
        sys.stderr.write(f'{err}\n')
        return 2
    run_shell(config, session, seed)
    return 0


async def _export_logs(
    pool: ClientProvider,
    args: argparse.Namespace,
    now: datetime,
    *,
    fetch_events: FetchEvents | None,
    executor: ThreadPoolExecutor,
) -> int:
    try:
        start_time, end_time = _window_from_args(args, now)
        filter_node = _local_filter(args.filter_pattern)
    except ValueError as err:
        sys.stderr.write(f'{err}\n')
        return 2
    config = _load_config_or_report(args.config_path)
    if config is None:
        return 1
    request = FetchRequest(
        log_group=args.log_group,
        start_time=start_time,
        end_time=end_time,
        profile=args.profile,
        region=args.region,
    )
    paths = await resolve_parquet_path(
        await pool.client('logs'),
        request,
        config,
        now=now,
        use_cache=not args.no_cache,
        fetch_events=fetch_events,
        executor=executor,
    )
    if not paths:
        sys.stderr.write('No events found for the requested range\n')
        return 0
    events = query_parquet_files_to_log_events(paths, filter_node)
    await run_blocking(executor, lambda: write_ndjson(events, sys.stdout))
    return 0


async def _export_tail(
    pool: ClientProvider,
    args: argparse.Namespace,
    now: datetime,
    *,
    fetch_events: FetchEvents | None,
    stream_events: StreamEvents | None,
) -> int:
    if len(args.log_groups) > MAX_LIVE_TAIL_LOG_GROUPS:
        sys.stderr.write(f'At most {MAX_LIVE_TAIL_LOG_GROUPS} log groups are supported, got {len(args.log_groups)}\n')
        return 2
    try:
        backfill_start = parse_time(args.backfill, now=now) if args.backfill is not None else None
    except ValueError as err:
        sys.stderr.write(f'{err}\n')
        return 2
    if backfill_start is not None and backfill_start >= now:
        sys.stderr.write(f'--backfill ({backfill_start.isoformat()}) must be in the past\n')
        return 2
    if _load_config_or_report(args.config_path) is None:
        return 1
    request = TailRequest(
        log_groups=tuple(args.log_groups),
        filter_pattern=args.filter_pattern,
        backfill_start=backfill_start,
        profile=args.profile,
        region=args.region,
    )
    events = iter_tail_events(
        await pool.client('logs'),
        request,
        now=now,
        fetch_events=fetch_events,
        stream_events=stream_events,
    )
    async with closing_stream(events) as stream:
        try:
            await stream_ndjson(stream, sys.stdout)
        except (KeyboardInterrupt, asyncio.CancelledError):
            return 0
    return 0


async def _export_groups(pool: ClientProvider, args: argparse.Namespace) -> int:
    if _load_config_or_report(args.config_path) is None:
        return 1
    logs = await pool.client('logs')
    groups = [group async for group in describe_log_groups(logs)]
    if args.pattern is not None:
        groups = resolve_group_pattern(args.pattern, groups)
    for group in groups:
        _write_json_line(_log_group_to_record(group))
    return 0


async def _resolve_summary_groups(
    logs: Any,
    patterns: Sequence[str],
    presets: Mapping[str, Sequence[str]],
) -> list[LogGroupInfo]:
    """Resolve patterns to log groups, deduplicated by name and in pattern order."""
    groups = [group async for group in describe_log_groups(logs)]
    expanded = expand_presets(patterns, presets) if patterns else []
    if not expanded:
        return groups
    resolved: dict[str, LogGroupInfo] = {}
    for pattern in expanded:
        for group in resolve_group_pattern(pattern, groups):
            resolved.setdefault(group.name, group)
    return list(resolved.values())


def _summary_to_record(report: RollupReport, *, window_label: str, source: str) -> dict[str, object]:
    return {
        'window': window_label,
        'source': source,
        'granularity': report.granularity.value,
        'scanned': report.scanned,
        'matched': report.matched,
        'distinct_shapes': report.distinct_shapes,
        'distinct_patterns': report.distinct_patterns,
        'bucket_labels': list(report.bucket_labels),
        'severity_totals': {severity.name.lower(): count for severity, count in report.severity_totals},
        'patterns': [
            {
                'key': pattern.key,
                'example': pattern.example,
                'severity': pattern.severity.name.lower(),
                'count': pattern.count,
                'first_seen': pattern.first_seen.isoformat(),
                'last_seen': pattern.last_seen.isoformat(),
                'merged_shapes': pattern.merged_shapes,
                'log_groups': dict(pattern.log_groups),
                'buckets': dict(pattern.buckets),
            }
            for pattern in report.patterns
        ],
    }


async def _export_summary(
    pool: ClientProvider,
    args: argparse.Namespace,
    now: datetime,
    *,
    fetch_events: FetchEvents | None,
    executor: ThreadPoolExecutor,
) -> int:
    try:
        start_time, end_time = _window_from_args(args, now)
        filter_node = _local_filter(args.filter_pattern)
    except ValueError as err:
        sys.stderr.write(f'{err}\n')
        return 2
    config = _load_config_or_report(args.config_path)
    if config is None:
        return 1
    logs = await pool.client('logs')
    names = [group.name for group in await _resolve_summary_groups(logs, args.patterns, config.presets)]
    if not names:
        sys.stderr.write('No log groups matched\n')
        return 1
    if len(names) > args.max_groups:
        dropped = names[args.max_groups :]
        sys.stderr.write(
            f'Capped at {args.max_groups} of {len(names)} matching groups; not fetched: {", ".join(dropped)}\n',
        )
        names = names[: args.max_groups]

    requests = [
        FetchRequest(
            log_group=name,
            start_time=start_time,
            end_time=end_time,
            profile=args.profile,
            region=args.region,
        )
        for name in names
    ]
    paths = await resolve_parquet_paths(
        logs,
        requests,
        config,
        now=now,
        use_cache=not args.no_cache,
        fetch_events=fetch_events,
        executor=executor,
    )
    if not paths:
        sys.stderr.write('No events found for the requested range\n')
        return 0

    report = await run_blocking(
        executor,
        lambda: roll_up(
            query_parquet_files_to_log_events(paths, filter_node),
            window=(start_time, end_time),
            granularity=Granularity(args.granularity),
            min_severity=Severity[args.level.upper()],
            limit=args.top,
            similarity=args.similarity if args.similarity > 0 else None,
        ),
    )
    window_label = _window_label(start_time, end_time)
    source = f'{len(paths)} of {len(names)} groups with events'
    table = render_markdown(
        report,
        title=f'{args.level.capitalize()}-and-above patterns',
        window_label=window_label,
        source=source,
    )
    _remember(
        HistoryKind.SUMMARY,
        title=f'summary of {", ".join(names)}',
        window=window_label,
        detail=table,
        args=args,
        now=now,
    )
    if args.output_format == 'json':
        _write_json_line(_summary_to_record(report, window_label=window_label, source=source))
        return 0
    sys.stdout.write(table)
    return 0


async def _export_insights(pool: ClientProvider, args: argparse.Namespace, now: datetime) -> int:
    try:
        start_time, end_time = _window_from_args(args, now)
    except ValueError as err:
        sys.stderr.write(f'{err}\n')
        return 2
    config = _load_config_or_report(args.config_path)
    if config is None:
        return 1
    logs = await pool.client('logs')
    groups = await _resolve_summary_groups(logs, args.patterns, config.presets)
    if not groups:
        sys.stderr.write('No log groups matched\n')
        return 1
    if len(groups) > args.max_groups:
        sys.stderr.write(
            f'Capped at {args.max_groups} of {len(groups)} matching groups; '
            f'not queried: {", ".join(group.name for group in groups[args.max_groups :])}\n',
        )
        groups = groups[: args.max_groups]
    names = [group.name for group in groups]

    if (refusal := _insights_preflight(groups, args, config, window=end_time - start_time, now=now)) is not None:
        return refusal

    try:
        validate_insights_request(args.query, start_time, end_time)
        result = await run_insights_query(
            logs,
            log_groups=names,
            query=args.query,
            start_time=start_time,
            end_time=end_time,
            limit=args.limit,
        )
    except (InsightsQueryError, ValueError) as err:
        sys.stderr.write(f'{err}\n')
        return 1

    _report_insights_cost(result, group_count=len(names))
    table = render_rows_markdown(result.columns, result.rows)
    _remember(
        HistoryKind.INSIGHTS,
        title=args.query,
        window=_window_label(start_time, end_time),
        detail=table,
        args=args,
        now=now,
    )
    if args.output_format == 'md':
        sys.stdout.write(table)
    else:
        for row in result.rows:
            _write_json_line(dict(row))
    return 0


def _insights_preflight(
    groups: Sequence[LogGroupInfo],
    args: argparse.Namespace,
    config: TailCWConfig,
    *,
    window: timedelta,
    now: datetime,
) -> int | None:
    """Report what the query is likely to scan, and refuse above the ceiling.

    Returns an exit code when the query must not run, or None to go ahead.
    """
    estimate = estimate_scan(groups, window=window, now=now)
    sys.stderr.write(f'{estimate.label()}\n')
    if args.dry_run:
        return 0
    ceiling = config.insights.confirm_above_gb
    if estimate.gigabytes > ceiling and not args.yes:
        sys.stderr.write(
            f'Above the {ceiling:g} GB ceiling. Re-run with --yes, '
            'or raise [insights].confirm_above_gb in config.toml.\n',
        )
        return 1
    return None


def _window_label(start_time: datetime, end_time: datetime) -> str:
    return f'{start_time.isoformat()} \u2192 {end_time.isoformat()}'


def _remember(
    kind: HistoryKind,
    *,
    title: str,
    window: str,
    detail: str,
    args: argparse.Namespace,
    now: datetime,
) -> None:
    """Record one question in the history the TUI also reads."""
    append(
        make_entry(
            kind,
            recorded=now,
            title=title,
            window=window,
            detail=detail,
            profile=getattr(args, 'profile', None),
        ),
    )


def _report_insights_cost(result: InsightsResult, *, group_count: int) -> None:
    """Write what the query scanned to stderr, because Insights bills on it."""
    gigabytes = result.bytes_scanned / 1_000_000_000
    sys.stderr.write(
        f'{len(result.rows)} rows from {group_count} groups; '
        f'{result.records_matched:,} of {result.records_scanned:,} records matched, '
        f'{gigabytes:.3f} GB scanned\n',
    )


def _alarm_to_record(alarm: AlarmSummary) -> dict[str, object]:
    return {
        'name': alarm.name,
        'state': alarm.state,
        'state_updated': alarm.state_updated.isoformat() if alarm.state_updated is not None else None,
        'state_reason': alarm.state_reason,
        'description': alarm.description,
        'namespace': alarm.namespace,
        'metric_name': alarm.metric_name,
        'dimensions': dict(alarm.dimensions),
        'statistic': alarm.statistic,
        'comparison': alarm.comparison,
        'threshold': alarm.threshold,
        'period_seconds': alarm.period_seconds,
        'datapoints_to_alarm': alarm.datapoints_to_alarm,
        'evaluation_periods': alarm.evaluation_periods,
        'actions_enabled': alarm.actions_enabled,
    }


async def _export_alarms(pool: ClientProvider, args: argparse.Namespace, now: datetime) -> int:
    try:
        start_time, end_time = _window_from_args(args, now)
    except ValueError as err:
        sys.stderr.write(f'{err}\n')
        return 2
    if _load_config_or_report(args.config_path) is None:
        return 1
    cloudwatch = await pool.client('cloudwatch')
    alarms = [alarm async for alarm in describe_alarms(cloudwatch, name_prefix=args.prefix, states=args.state)]
    counts: dict[str, int] = {}
    for alarm in alarms:
        record = _alarm_to_record(alarm)
        if args.history:
            transitions = [
                transition
                async for transition in describe_alarm_history(
                    cloudwatch,
                    alarm.name,
                    start_time=start_time,
                    end_time=end_time,
                )
            ]
            counts[alarm.name] = len(transitions)
            record['transitions'] = len(transitions)
            record['history'] = [
                {'moment': transition.moment.isoformat(), 'summary': transition.summary} for transition in transitions
            ]
        _write_json_line(record)
    if not alarms:
        sys.stderr.write('No alarms matched\n')
        return 0
    _remember(
        HistoryKind.ALARMS,
        title=f'{len(alarms)} alarms' + (f', {sum(counts.values())} transitions' if counts else ''),
        window=_window_label(start_time, end_time),
        detail=render_alarm_markdown(alarms, counts),
        args=args,
        now=now,
    )
    return 0


def _parse_dimensions(values: Sequence[str] | None) -> list[tuple[str, str]]:
    """Parse repeated ``NAME=VALUE`` flags.

    Raises:
        ValueError: A value has no ``=``.
    """
    parsed: list[tuple[str, str]] = []
    for value in values or []:
        name, separator, dimension_value = value.partition('=')
        if not separator or not name:
            msg = f'--dimension expects NAME=VALUE, got {value!r}'
            raise ValueError(msg)
        parsed.append((name, dimension_value))
    return parsed


async def _export_metrics(pool: ClientProvider, args: argparse.Namespace, now: datetime) -> int:
    try:
        start_time, end_time = _window_from_args(args, now)
        dimensions = _parse_dimensions(args.dimension)
    except ValueError as err:
        sys.stderr.write(f'{err}\n')
        return 2
    if _load_config_or_report(args.config_path) is None:
        return 1

    # The console's metrics[] shorthand is flat: namespace, metric, then dimension pairs.
    shorthand: list[Any] = [args.namespace, args.metric]
    for name, value in dimensions:
        shorthand.extend([name, value])
    queries = build_metric_data_queries(
        [shorthand],
        widget_stat=args.stat,
        widget_period=args.period,
        default_period=DEFAULT_PERIOD_SECONDS,
    )
    series = await fetch_metric_data(await pool.client('cloudwatch'), queries, start_time, end_time)
    if not any(item.values for item in series):
        sys.stderr.write('No datapoints in the requested range\n')
    for item in series:
        _write_json_line(_metric_series_to_record(item))
    return 0


def _metric_series_to_record(series: MetricSeries) -> dict[str, object]:
    return {
        'id': series.id,
        'label': series.label,
        'datapoints': [
            {'timestamp': timestamp.isoformat(), 'value': value}
            for timestamp, value in zip(series.timestamps, series.values, strict=False)
        ],
    }


async def _export_dashboards(pool: ClientProvider, args: argparse.Namespace) -> int:
    if _load_config_or_report(args.config_path) is None:
        return 1
    for summary in await list_dashboards(await pool.client('cloudwatch')):
        _write_json_line(_dashboard_summary_to_record(summary))
    return 0


async def _export_dashboard(pool: ClientProvider, args: argparse.Namespace) -> int:
    if not args.demo and args.name is None and args.dashboard_file is None:
        sys.stderr.write('Provide a dashboard name, --file, or --demo\n')
        return 2
    if _load_config_or_report(args.config_path) is None:
        return 1
    try:
        if args.demo:
            dashboard = demo_dashboard()
        elif args.dashboard_file is not None:
            dashboard = load_dashboard_file(args.dashboard_file)
        else:
            dashboard = await get_dashboard(await pool.client('cloudwatch'), str(args.name))
    except ValueError as err:
        sys.stderr.write(f'{err}\n')
        return 1
    sys.stdout.write(json.dumps(dashboard_to_dict(dashboard), separators=(',', ':')) + '\n')
    return 0


async def _dispatch_export(
    pool: ClientProvider,
    args: argparse.Namespace,
    now: datetime,
    *,
    executor: ThreadPoolExecutor,
    fetch_events: FetchEvents | None,
    stream_events: StreamEvents | None,
) -> int:
    handlers: dict[str, Callable[[], Awaitable[int]]] = {
        'logs': lambda: _export_logs(pool, args, now, fetch_events=fetch_events, executor=executor),
        'tail': lambda: _export_tail(pool, args, now, fetch_events=fetch_events, stream_events=stream_events),
        'groups': lambda: _export_groups(pool, args),
        'summary': lambda: _export_summary(pool, args, now, fetch_events=fetch_events, executor=executor),
        'insights': lambda: _export_insights(pool, args, now),
        'alarms': lambda: _export_alarms(pool, args, now),
        'metrics': lambda: _export_metrics(pool, args, now),
        'dashboards': lambda: _export_dashboards(pool, args),
        'dashboard': lambda: _export_dashboard(pool, args),
    }
    return await handlers[args.export_command]()


async def _run_export_command(
    args: argparse.Namespace,
    now: datetime,
    parser: argparse.ArgumentParser,
    *,
    fetch_events: FetchEvents | None,
    stream_events: StreamEvents | None,
) -> int:
    """Run one export subcommand. Argparse rejects unknown names, so only a missing one."""
    if args.export_command is None:
        parser.print_help(sys.stderr)
        return 2
    with blocking_pool() as executor:
        async with client_pool(profile_name=args.profile, region_name=args.region) as pool:
            return await _dispatch_export(
                pool,
                args,
                now,
                executor=executor,
                fetch_events=fetch_events,
                stream_events=stream_events,
            )


def run_cli(
    argv: Sequence[str] | None,
    run_shell: RunShell | None = None,
    *,
    fetch_events: FetchEvents | None = None,
    stream_events: StreamEvents | None = None,
    is_tty: bool | None = None,
) -> int:
    """Parse arguments and either open the shell or write an export to stdout.

    Bare ``tail-cw`` opens the shell on a TTY and prints help with exit code 2
    otherwise, so piping into a script still gets usable output rather than a
    terminal app. Returns the process exit code.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    now = datetime.now(tz=UTC)
    interactive = sys.stdout.isatty() if is_tty is None else is_tty
    match args.command:
        case 'export':
            return asyncio.run(
                _run_export_command(args, now, parser, fetch_events=fetch_events, stream_events=stream_events),
            )
        case 'logs' | 'tail' | 'dash':
            return _run_shell_command(args, now, run_shell)
        case _ if interactive:
            return _run_shell_command(args, now, run_shell)
        case _:
            parser.print_help(sys.stderr)
            return 2
