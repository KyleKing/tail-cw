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

from tail_cw.aws.alarms import AlarmSummary, describe_alarm_history, describe_alarms
from tail_cw.aws.client import ClientProvider, client_pool, fetch_log_events
from tail_cw.aws.dashboards import (
    DashboardSummary,
    dashboard_to_dict,
    get_dashboard,
    list_dashboards,
    load_dashboard_file,
)
from tail_cw.aws.events import LogEvent
from tail_cw.aws.insights import (
    DOLLARS_PER_GB,
    InsightsQueryError,
    InsightsResult,
    QueryLanguage,
    estimate_scan,
    measure_group_rates,
    names_its_own_groups,
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
    list_metric_definitions,
)
from tail_cw.aws.xray import XRayTraceSummary, batch_get_traces, iter_trace_summary_pages, scan_cost_usd
from tail_cw.cache.storage import CacheStatus, LogCache, generate_cache_key
from tail_cw.cache.window import Segment, plan_segments
from tail_cw.concurrency import closing_stream, consume_in_thread, fetch_pool, run_blocking
from tail_cw.config import TailCWConfig, get_default_cache_dir, load_config
from tail_cw.demo import DEMO_LOG_GROUP, demo_dashboard
from tail_cw.history import HistoryKind, append, make_entry
from tail_cw.parser import DEFAULT_WINDOW, build_parser
from tail_cw.query.engine import query_parquet_files_to_log_events
from tail_cw.query.expression import parse_query, portable_filter_pattern
from tail_cw.query.otlp import trace_error_summary, trace_groups_to_otlp, xray_trace_summary, xray_traces_to_otlp
from tail_cw.query.parser import FilterNode
from tail_cw.query.report import render_alarm_markdown, render_markdown, render_rows_markdown
from tail_cw.query.rollup import Granularity, RollupReport, roll_up
from tail_cw.query.severity import Severity
from tail_cw.query.trace import query_traces_from_parquet_files
from tail_cw.recents import load_recents, record_selection, save_recents

FetchEvents = Callable[..., AsyncIterator[LogEvent]]
StreamEvents = Callable[..., AsyncIterator[LogEvent]]
ShellView = Literal['groups', 'logs', 'tail', 'dashboards', 'dashboard']

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


def _tail_backfill(backfill: str | None, *, now: datetime) -> datetime | None:
    """Parse the backfill window, which has to be in the past to mean anything.

    Raises:
        ValueError: If the value is unparseable or not in the past.
    """
    if backfill is None:
        return None
    start = parse_time(backfill, now=now)
    if start >= now:
        msg = f'--backfill ({start.isoformat()}) must be in the past'
        raise ValueError(msg)
    return start


def server_side_pattern(filter_pattern: str | None) -> str | None:
    """Translate a filter for AWS, refusing what CloudWatch would answer wrongly.

    A live tail is the one path that hands a pattern to CloudWatch, and CloudWatch
    silently ignores its ``?`` any-of terms when they are mixed with anything else. So a
    local-only expression is rejected here rather than sent and quietly mismatched; the
    events it would have filtered are still filtered locally.

    Raises:
        ValueError: If the filter cannot be expressed as a CloudWatch pattern.
    """
    if not filter_pattern:
        return None
    portable = portable_filter_pattern(parse_query(filter_pattern))
    if portable.pattern is None:
        msg = f'{filter_pattern!r} cannot be sent to CloudWatch: {portable.reason}'
        raise ValueError(msg)
    return portable.pattern


def _local_filter(filter_pattern: str | None) -> FilterNode | None:
    """Parse a ``--filter`` value for local evaluation against cached events."""
    return parse_query(filter_pattern) if filter_pattern else None


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
    limiter: asyncio.Semaphore,
) -> list[Path]:
    """Resolve every segment of one window, several at a time, in window order."""
    effective_fetch = fetch_events if fetch_events is not None else fetch_log_events

    async def resolve(segment: Segment) -> Path | None:
        async with limiter:
            return await _resolve_segment(
                client,
                request,
                segment,
                cache,
                use_cache=use_cache,
                fetch_events=effective_fetch,
                executor=executor,
            )

    async with asyncio.TaskGroup() as group:
        tasks = [
            group.create_task(resolve(segment))
            for segment in plan_segments(request.start_time, request.end_time, now=now)
        ]
    paths: list[Path] = []
    for task in tasks:
        path = task.result()
        if path is not None and path not in paths:
            paths.append(path)
    return paths


def _segment_limiter(config: TailCWConfig) -> asyncio.Semaphore:
    """Bound segment fetches for one command, across every log group in it.

    Built here rather than held at module level, so it binds to the loop that is
    running rather than to whichever one imported this module first.
    """
    return asyncio.Semaphore(max(1, config.fetch.max_concurrent_segments))


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
            limiter=_segment_limiter(config),
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

    Every segment of every request competes for the same
    ``[fetch].max_concurrent_segments`` slots, because each one in flight holds a
    thread of ``executor`` until it finishes writing. A failure cancels the
    siblings rather than leaving them to finish writing into a cache nobody will
    read.
    """
    if not requests:
        return []
    resolved_now = now if now is not None else datetime.now(UTC)
    limiter = _segment_limiter(config)
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
                limiter=limiter,
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
        'log_group_class': group.log_group_class,
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


def expand_filter(text: str | None, filters: Mapping[str, str]) -> str | None:
    """Replace a whole-filter ``@name`` reference with its configured expression.

    Only the whole filter, not a term inside one: a filter is one expression rather than
    a list, so there is no position where a partial substitution would be unambiguous.
    Anything not starting with ``@`` passes through untouched.

    Raises:
        ValueError: When the reference names no configured filter. Expanding to nothing
            would silently search for everything.
    """
    if text is None or not text.startswith('@'):
        return text
    name = text.removeprefix('@').strip()
    expression = filters.get(name)
    if expression is None:
        known = ', '.join(f'@{key}' for key in sorted(filters)) or 'none configured'
        msg = f'Unknown filter {text!r}; configured filters: {known}'
        raise ValueError(msg)
    return expression


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
    *,
    demo: bool = False,
) -> ShellSeed:
    expanded = expand_presets(patterns, presets)
    if demo:
        # The demo has one group, so an empty pattern opens on it rather than on a
        # browser that would list it and nothing else.
        return ShellSeed(view=view, targets=tuple(expanded) or (DEMO_LOG_GROUP,), demo=True)
    return ShellSeed(view=view if expanded else 'groups', targets=tuple(expanded))


def seed_from_args(args: argparse.Namespace, presets: Mapping[str, Sequence[str]] | None = None) -> ShellSeed:
    """Choose the opening view from the subcommand and its arguments.

    A log group argument naming an unknown preset propagates ``ValueError`` from
    :func:`expand_presets`.
    """
    known = presets if presets is not None else {}
    match args.command:
        case 'logs':
            return _log_view_seed('logs', args.patterns, known, demo=getattr(args, 'demo', False))
        case 'tail':
            return _log_view_seed('tail', args.patterns, known, demo=getattr(args, 'demo', False))
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
        session.filter_pattern = expand_filter(session.filter_pattern, config.filters)
    except ValueError as err:
        sys.stderr.write(f'{err}\n')
        return 2
    _remember_literal_groups(seed.targets, profile=args.profile)
    run_shell(config, session, seed)
    return 0


_PATTERN_CHARACTERS = frozenset('*?[')


def _remember_literal_groups(targets: Sequence[str], *, profile: str | None) -> None:
    """Record the names typed on the command line, so completion can offer them back.

    Only literal names: a glob is not a group, and recording one would offer a pattern
    where a name belongs. Failing to write is not worth an error, since the history is a
    convenience and the command it decorates has already been asked for.
    """
    literal = [name for name in targets if not (_PATTERN_CHARACTERS & set(name))]
    if not literal:
        return
    try:
        save_recents(record_selection(load_recents(), literal, profile=profile))
    except OSError:
        return


async def _export_logs(
    pool: ClientProvider,
    args: argparse.Namespace,
    now: datetime,
    *,
    fetch_events: FetchEvents | None,
    executor: ThreadPoolExecutor,
) -> int:
    config = _load_config_or_report(args.config_path)
    if config is None:
        return 1
    try:
        start_time, end_time = _window_from_args(args, now)
        filter_node = _local_filter(expand_filter(args.filter_pattern, config.filters))
    except ValueError as err:
        sys.stderr.write(f'{err}\n')
        return 2
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
        backfill_start = _tail_backfill(args.backfill, now=now)
        sent_pattern = server_side_pattern(args.filter_pattern)
    except ValueError as err:
        sys.stderr.write(f'{err}\n')
        return 2
    if _load_config_or_report(args.config_path) is None:
        return 1
    request = TailRequest(
        log_groups=tuple(args.log_groups),
        filter_pattern=sent_pattern,
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
    language = QueryLanguage(args.language.upper())
    logs = await pool.client('logs')
    resolved = await _insights_targets(logs, args, config, language, window=(start_time, end_time), now=now)
    if isinstance(resolved, int):
        return resolved
    names = resolved

    try:
        validate_insights_request(args.query, start_time, end_time, language.value)
        result = await run_insights_query(
            logs,
            log_groups=names,
            query=args.query,
            start_time=start_time,
            end_time=end_time,
            limit=args.limit,
            language=language,
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


async def _export_trace(
    pool: ClientProvider,
    args: argparse.Namespace,
    now: datetime,
    *,
    fetch_events: FetchEvents | None,
    executor: ThreadPoolExecutor,
) -> int:
    """Collect one trace across every matching group and write it as OTLP JSON.

    A trace spans services, so every group is read together; grouping is
    blocking DuckDB work and runs on the pool.
    """
    try:
        start_time, end_time = _window_from_args(args, now)
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
    names = names[: args.max_groups]
    paths = await resolve_parquet_paths(
        logs,
        [
            FetchRequest(
                log_group=name,
                start_time=start_time,
                end_time=end_time,
                profile=args.profile,
                region=args.region,
            )
            for name in names
        ],
        config,
        now=now,
        use_cache=not args.no_cache,
        fetch_events=fetch_events,
        executor=executor,
    )
    groups = await run_blocking(
        executor,
        lambda: query_traces_from_parquet_files(
            paths,
            trace_id=args.trace_id,
            trace_id_fields=list(config.trace.trace_id_fields),
        ),
    )
    if not groups:
        sys.stderr.write(f'Trace {args.trace_id} has no spans in {len(paths)} of {len(names)} groups\n')
        return 1
    for group in groups:
        sys.stderr.write(f'{trace_error_summary(group)}\n')
    json.dump(trace_groups_to_otlp(groups), sys.stdout)
    sys.stdout.write('\n')
    return 0


def _xray_summary_to_record(summary: XRayTraceSummary) -> dict[str, object]:
    return {
        'trace_id': summary.trace_id,
        'start_time': summary.start_time.isoformat() if summary.start_time is not None else None,
        'duration_seconds': summary.duration_seconds,
        'response_time_seconds': summary.response_time_seconds,
        'has_fault': summary.has_fault,
        'has_error': summary.has_error,
        'has_throttle': summary.has_throttle,
        'is_partial': summary.is_partial,
        'entry_point': summary.entry_point,
        'services': list(summary.service_names),
        'http_method': summary.http_method,
        'http_url': summary.http_url,
        'http_status': summary.http_status,
    }


async def _export_xray(pool: ClientProvider, args: argparse.Namespace, now: datetime) -> int:
    """Write one NDJSON row per trace in the window, for pivoting into ``xray-trace``."""
    try:
        start_time, end_time = _window_from_args(args, now)
    except ValueError as err:
        sys.stderr.write(f'{err}\n')
        return 2
    xray = await pool.client('xray')
    pages = iter_trace_summary_pages(
        xray,
        start_time=start_time,
        end_time=end_time,
        filter_expression=args.filter_expression,
        sampling=args.sampling,
    )
    written = 0
    processed = 0
    async for page in pages:
        processed += page.traces_processed
        for summary in page.summaries:
            _write_json_line(_xray_summary_to_record(summary))
            written += 1
            if written >= args.limit:
                break
        if written >= args.limit:
            sys.stderr.write(f'Stopped at --limit {args.limit}; widen it or narrow --start to see more\n')
            break
    _report_xray_cost(processed, written=written)
    if written == 0:
        sys.stderr.write(f'No X-Ray traces in {_window_label(start_time, end_time)}\n')
        return 1
    return 0


def _report_xray_cost(processed: int, *, written: int) -> None:
    """Name what the scan cost, because a filter expression does not reduce it.

    X-Ray bills every trace it looks at, matched or not, so a wide window is expensive
    however narrow the expression is. Only a shorter window is cheaper.
    """
    if processed <= 0:
        return
    sys.stderr.write(
        f'{written} traces written, {processed} scanned, about ${scan_cost_usd(processed):.2f} '
        f'past the free million a month\n',
    )


async def _export_xray_trace(pool: ClientProvider, args: argparse.Namespace) -> int:
    """Write full segment documents for named traces as one OTLP document."""
    xray = await pool.client('xray')
    traces = await batch_get_traces(xray, args.trace_ids)
    found = {trace.trace_id for trace in traces}
    for missing in (trace_id for trace_id in args.trace_ids if trace_id not in found):
        sys.stderr.write(f'X-Ray has no segments for {missing}\n')
    if not traces:
        return 1
    for trace in traces:
        sys.stderr.write(f'{xray_trace_summary(trace)}\n')
    json.dump(xray_traces_to_otlp(traces), sys.stdout)
    sys.stdout.write('\n')
    return 0


async def _insights_targets(
    logs: Any,
    args: argparse.Namespace,
    config: TailCWConfig,
    language: QueryLanguage,
    *,
    window: tuple[datetime, datetime],
    now: datetime,
) -> list[str] | int:
    """Resolve the groups to query and run the cost gate, or return an exit code.

    Two paths, because ``StartQuery`` takes the groups either as a parameter or from a
    clause inside the query, never both.
    """
    if names_its_own_groups(args.query, language):
        refusal = _self_selecting_preflight(args, language)
        return refusal if refusal is not None else []
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
    start_time, end_time = window
    rates = await measure_group_rates(logs, names, start=start_time, end=end_time)
    refusal = _insights_preflight(groups, args, config, window=end_time - start_time, now=now, rates=rates)
    return refusal if refusal is not None else names


def _self_selecting_preflight(args: argparse.Namespace, language: QueryLanguage) -> int | None:
    """Gate a query whose sources are inside it, where no estimate is possible.

    The scan estimate samples the log groups it was told about, and a SQL ``FROM`` or a
    PPL ``SOURCE`` names them in text this tool does not parse. So the ceiling that
    normally protects a billed query cannot be applied, and the honest response is to say
    so and require ``--yes`` rather than to run blind or to pretend at a number.

    Returns an exit code when the query must not run, or None to go ahead.
    """
    if args.patterns:
        sys.stderr.write(
            f'A {language.value} query naming its own sources takes no log group arguments; '
            f'drop {", ".join(args.patterns)}\n',
        )
        return 2
    sys.stderr.write(
        f'No scan estimate: this {language.value} query selects its own log groups, '
        'so there is nothing to sample before running it.\n',
    )
    if args.dry_run:
        return 0
    if not args.yes:
        sys.stderr.write(
            f'Re-run with --yes to run it anyway, or use --language cwli with log group '
            f'arguments to get an estimate first. Insights bills ${DOLLARS_PER_GB:g} per GB scanned.\n',
        )
        return 1
    return None


def _insights_preflight(
    groups: Sequence[LogGroupInfo],
    args: argparse.Namespace,
    config: TailCWConfig,
    *,
    window: timedelta,
    now: datetime,
    rates: Mapping[str, float] | None = None,
) -> int | None:
    """Report what the query is likely to scan, and refuse above the ceiling.

    Returns an exit code when the query must not run, or None to go ahead.
    """
    estimate = estimate_scan(groups, window=window, now=now, rates=rates)
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
    # A self-selecting query passes no group list, so counting it would report zero.
    source = f'{group_count} groups' if group_count else 'the groups the query named'
    sys.stderr.write(
        f'{len(result.rows)} rows from {source}; '
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


async def _export_dimensions(pool: ClientProvider, args: argparse.Namespace) -> int:
    """Write the dimension sets a namespace publishes, so a query can name one.

    ``ApiRequestLatencyMs`` carrying only ``Method`` and ``StatusClass`` is
    otherwise only visible in the emitter's source.
    """
    if _load_config_or_report(args.config_path) is None:
        return 1
    cloudwatch = await pool.client('cloudwatch')
    definitions = list_metric_definitions(cloudwatch, namespace=args.namespace, metric_name=args.metric)
    count = 0
    async for definition in definitions:
        count += 1
        _write_json_line(
            {
                'namespace': definition.namespace,
                'metric': definition.name,
                'dimension_names': list(definition.dimension_names),
                'dimensions': dict(definition.dimensions),
            },
        )
    if not count:
        sys.stderr.write(f'No metrics published in {args.namespace}\n')
        return 1
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


def _cache_status_record(status: CacheStatus) -> dict[str, object]:
    return {
        'cache_dir': str(status.cache_dir),
        'files': status.files,
        'bytes_used': status.bytes_used,
        'bytes_limit': status.bytes_limit,
        'fraction_used': round(status.fraction_used, 4),
        'oldest': status.oldest.isoformat() if status.oldest is not None else None,
        'newest': status.newest.isoformat() if status.newest is not None else None,
        'entries': status.entries,
        'stale_entries': status.stale_entries,
        'orphan_files': status.orphan_files,
        'default_ttl_seconds': status.default_ttl_seconds,
    }


def _run_cache_command(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    """Report on the local cache. Reads only, so nothing here evicts or writes."""
    if args.cache_command is None:
        parser.print_help(sys.stderr)
        return 2
    config = _load_config_or_report(args.config_path)
    if config is None:
        return 1
    with open_log_cache(config) as cache:
        _write_json_line(_cache_status_record(cache.status()))
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
        'trace': lambda: _export_trace(pool, args, now, fetch_events=fetch_events, executor=executor),
        'xray': lambda: _export_xray(pool, args, now),
        'xray-trace': lambda: _export_xray_trace(pool, args),
        'alarms': lambda: _export_alarms(pool, args, now),
        'metrics': lambda: _export_metrics(pool, args, now),
        'dimensions': lambda: _export_dimensions(pool, args),
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
    # One pool for the whole export path, sized for the fetch: a CLI export runs one
    # blocking call at a time, so nothing here can starve a query the way the TUI can.
    with fetch_pool() as executor:
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
    """Parse arguments, then run the command they name."""
    parser = build_parser()
    return dispatch(
        parser.parse_args(argv),
        parser,
        run_shell,
        fetch_events=fetch_events,
        stream_events=stream_events,
        is_tty=is_tty,
    )


def dispatch(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
    run_shell: RunShell | None = None,
    *,
    fetch_events: FetchEvents | None = None,
    stream_events: StreamEvents | None = None,
    is_tty: bool | None = None,
) -> int:
    """Open the shell or write an export to stdout, from already-parsed arguments.

    Bare ``tail-cw`` opens the shell on a TTY and prints help with exit code 2
    otherwise, so piping into a script still gets usable output rather than a
    terminal app. Returns the process exit code.
    """
    now = datetime.now(tz=UTC)
    interactive = sys.stdout.isatty() if is_tty is None else is_tty
    match args.command:
        case 'export':
            return asyncio.run(
                _run_export_command(args, now, parser, fetch_events=fetch_events, stream_events=stream_events),
            )
        case 'cache':
            return _run_cache_command(args, parser)
        case 'logs' | 'tail' | 'dash':
            return _run_shell_command(args, now, run_shell)
        case _ if interactive:
            return _run_shell_command(args, now, run_shell)
        case _:
            parser.print_help(sys.stderr)
            return 2
