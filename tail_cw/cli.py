"""Command line interface for tail-cw.

Provides the argparse subcommand parser, time parsing helpers, and the fetch
pipeline that connects the AWS client, the Parquet cache, and the output
surfaces (NDJSON to stdout or the Textual TUI). This module intentionally does
not import Textual; the TUI runner is injected by ``tail_cw.__main__``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from itertools import chain
from pathlib import Path
from typing import Literal

from beartype.typing import Protocol

from tail_cw.aws.client import LogEvent, fetch_log_events
from tail_cw.aws.dashboards import (
    DashboardSummary,
    dashboard_to_dict,
    get_dashboard,
    list_dashboards,
    load_dashboard_file,
)
from tail_cw.aws.live_tail import MAX_LIVE_TAIL_LOG_GROUPS, stream_live_tail
from tail_cw.aws.log_groups import LogGroupInfo, describe_log_groups, resolve_group_pattern
from tail_cw.cache.storage import LogCache, generate_cache_key, read_parquet_to_log_events
from tail_cw.config import TailCWConfig, get_default_cache_dir, load_config
from tail_cw.demo import demo_dashboard

FetchEvents = Callable[..., Iterator[LogEvent]]
StreamEvents = Callable[..., Iterator[LogEvent]]
ShellView = Literal['groups', 'logs', 'tail', 'dashboards', 'dashboard']

DEFAULT_WINDOW = '1h'
DEFAULT_DASHBOARD_WINDOW = '3h'

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
        filter_pattern: Optional CloudWatch filter pattern.
        profile: Optional AWS profile name.
        region: Optional AWS region name.
    """

    log_group: str
    start_time: datetime
    end_time: datetime
    filter_pattern: str | None = None
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
    export_sub = export.add_subparsers(dest='export_command')

    export_logs = export_sub.add_parser('logs', help='Write log events for a time range as NDJSON.')
    export_logs.add_argument('log_group', help='CloudWatch log group name (e.g. /aws/lambda/my-function)')
    _add_aws_flags(export_logs)
    _add_window_flags(export_logs, default_start=DEFAULT_WINDOW)
    export_logs.add_argument(
        '--no-cache',
        dest='no_cache',
        action='store_true',
        help='Bypass the cache read (results are still written to the cache)',
    )

    export_tail = export_sub.add_parser('tail', help='Stream live log events as NDJSON (Ctrl+C to stop).')
    export_tail.add_argument('log_groups', nargs='+', help='One or more CloudWatch log group names (max 10)')
    _add_aws_flags(export_tail)
    export_tail.add_argument('--filter', dest='filter_pattern', default=None, help='CloudWatch Logs filter pattern')
    export_tail.add_argument(
        '--backfill',
        default=None,
        help='Emit historical events for this window (e.g. 15m) before streaming live',
    )

    export_groups = export_sub.add_parser('groups', help='Write log group metadata as NDJSON.')
    export_groups.add_argument('pattern', nargs='?', default=None, help='Name, prefix, or glob to match')
    _add_aws_flags(export_groups)

    export_dashboards = export_sub.add_parser('dashboards', help='Write the account dashboard list as NDJSON.')
    _add_aws_flags(export_dashboards)

    export_dashboard = export_sub.add_parser('dashboard', help='Write one parsed dashboard structure as JSON.')
    export_dashboard.add_argument('name', nargs='?', default=None, help='Dashboard name (omit with --file or --demo)')
    _add_aws_flags(export_dashboard)
    export_dashboard.add_argument(
        '--demo',
        dest='demo',
        action='store_true',
        help='Emit the synthetic demo dashboard (no AWS calls)',
    )
    export_dashboard.add_argument(
        '--file',
        dest='dashboard_file',
        type=Path,
        default=None,
        help='Load a local dashboard JSON file (same schema as a CloudWatch DashboardBody)',
    )
    return parser


def iter_tail_events(
    request: TailRequest,
    *,
    now: datetime,
    fetch_events: FetchEvents | None = None,
    stream_events: StreamEvents | None = None,
) -> Iterator[LogEvent]:
    """Yield backfill events (when requested) followed by the live stream.

    The same filter pattern is pushed server-side to both FilterLogEvents
    (backfill) and StartLiveTail (``logEventFilterPattern``). The live path
    does not touch the Parquet cache.
    """
    effective_fetch = fetch_events if fetch_events is not None else fetch_log_events
    effective_stream = stream_events if stream_events is not None else stream_live_tail
    if request.backfill_start is not None:
        for log_group in request.log_groups:
            yield from effective_fetch(
                log_group,
                request.backfill_start,
                now,
                filter_pattern=request.filter_pattern,
                profile_name=request.profile,
                region_name=request.region,
            )
    yield from effective_stream(
        request.log_groups,
        filter_pattern=request.filter_pattern,
        profile_name=request.profile,
        region_name=request.region,
    )


def open_log_cache(config: TailCWConfig) -> LogCache:
    """Open the configured log cache. Close it, or use it as a context manager."""
    return LogCache(
        request_cache_dir(config),
        size_limit_mb=config.cache.size_limit_mb,
        default_ttl_seconds=config.cache.default_ttl_seconds,
        eviction_policy=config.cache.eviction_policy,
        compression_level=config.parquet.compression_level,
        row_group_size=config.parquet.row_group_size,
        infer_schema_length=config.parquet.infer_schema_length,
    )


def _resolve_into_cache(
    request: FetchRequest,
    cache: LogCache,
    *,
    use_cache: bool,
    fetch_events: FetchEvents | None,
) -> Path | None:
    effective_fetch = fetch_events if fetch_events is not None else fetch_log_events
    cache_key = generate_cache_key(
        request.log_group,
        request.start_time,
        request.end_time,
        filter_pattern=request.filter_pattern,
        region_name=request.region,
        profile_name=request.profile,
    )
    if use_cache and (cached_path := cache.get_parquet_path(cache_key)) is not None:
        return cached_path
    events = iter(
        effective_fetch(
            request.log_group,
            request.start_time,
            request.end_time,
            filter_pattern=request.filter_pattern,
            profile_name=request.profile,
            region_name=request.region,
        ),
    )
    try:
        first_event = next(events)
    except StopIteration:
        return None
    cache.write(chain([first_event], events), cache_key)
    return cache.get_parquet_path(cache_key)


def resolve_parquet_path(
    request: FetchRequest,
    config: TailCWConfig,
    *,
    use_cache: bool = True,
    fetch_events: FetchEvents | None = None,
) -> Path | None:
    """Return the cached Parquet path for a request, fetching from AWS on miss.

    Returns None when the request matches no events.
    """
    with open_log_cache(config) as cache:
        return _resolve_into_cache(request, cache, use_cache=use_cache, fetch_events=fetch_events)


def resolve_parquet_paths(
    requests: Sequence[FetchRequest],
    config: TailCWConfig,
    *,
    use_cache: bool = True,
    fetch_events: FetchEvents | None = None,
    max_workers: int = 4,
) -> list[Path]:
    """Resolve several fetches in parallel, dropping the ones with no events.

    One Parquet file per request keeps each log group independently cacheable;
    the caller merges them at read time. Results keep the request order rather
    than completion order so the caller's group list stays meaningful.

    Every worker shares one ``LogCache``. Separate instances over the same
    directory delete each other's not-yet-referenced Parquet files during orphan
    cleanup, which silently drops groups from the result.
    """
    if not requests:
        return []
    with open_log_cache(config) as cache:
        resolve = partial(_resolve_into_cache, cache=cache, use_cache=use_cache, fetch_events=fetch_events)
        if len(requests) == 1:
            path = resolve(requests[0])
            return [] if path is None else [path]
        with ThreadPoolExecutor(max_workers=min(max_workers, len(requests))) as pool:
            return [path for path in pool.map(resolve, requests) if path is not None]


def request_cache_dir(config: TailCWConfig) -> Path:
    """Return the configured cache directory, falling back to the XDG default."""
    return config.cache.cache_dir if config.cache.cache_dir is not None else get_default_cache_dir()


def _event_to_record(event: LogEvent) -> dict[str, str]:
    return {
        'timestamp': event.timestamp.isoformat(),
        'log_group': event.log_group,
        'log_stream': event.log_stream,
        'message': event.message,
        'event_id': event.event_id,
    }


def write_ndjson(events: Iterable[LogEvent], stream: SupportsWriteStr) -> int:
    """Write log events to a stream as NDJSON and return the number written."""
    count = 0
    for event in events:
        stream.write(json.dumps(_event_to_record(event), separators=(',', ':')) + '\n')
        count += 1
    return count


def stream_ndjson(events: Iterable[LogEvent], stream: SupportsWriteFlushStr) -> int:
    """Write log events as NDJSON, flushing after every line for live consumers."""
    count = 0
    for event in events:
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


def _export_logs(args: argparse.Namespace, now: datetime, *, fetch_events: FetchEvents | None) -> int:
    try:
        start_time, end_time = _window_from_args(args, now)
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
        filter_pattern=args.filter_pattern,
        profile=args.profile,
        region=args.region,
    )
    parquet_path = resolve_parquet_path(request, config, use_cache=not args.no_cache, fetch_events=fetch_events)
    if parquet_path is None:
        sys.stderr.write('No events found for the requested range\n')
        return 0
    write_ndjson(read_parquet_to_log_events(parquet_path), sys.stdout)
    return 0


def _export_tail(
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
    events = iter_tail_events(request, now=now, fetch_events=fetch_events, stream_events=stream_events)
    try:
        stream_ndjson(events, sys.stdout)
    except KeyboardInterrupt:
        return 0
    return 0


def _export_groups(args: argparse.Namespace) -> int:
    if _load_config_or_report(args.config_path) is None:
        return 1
    groups = list(describe_log_groups(profile_name=args.profile, region_name=args.region))
    if args.pattern is not None:
        groups = resolve_group_pattern(args.pattern, groups)
    for group in groups:
        _write_json_line(_log_group_to_record(group))
    return 0


def _export_dashboards(args: argparse.Namespace) -> int:
    if _load_config_or_report(args.config_path) is None:
        return 1
    for summary in list_dashboards(profile_name=args.profile, region_name=args.region):
        _write_json_line(_dashboard_summary_to_record(summary))
    return 0


def _export_dashboard(args: argparse.Namespace) -> int:
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
            dashboard = get_dashboard(str(args.name), profile_name=args.profile, region_name=args.region)
    except ValueError as err:
        sys.stderr.write(f'{err}\n')
        return 1
    sys.stdout.write(json.dumps(dashboard_to_dict(dashboard), separators=(',', ':')) + '\n')
    return 0


def _run_export_command(
    args: argparse.Namespace,
    now: datetime,
    parser: argparse.ArgumentParser,
    *,
    fetch_events: FetchEvents | None,
    stream_events: StreamEvents | None,
) -> int:
    match args.export_command:
        case 'logs':
            return _export_logs(args, now, fetch_events=fetch_events)
        case 'tail':
            return _export_tail(args, now, fetch_events=fetch_events, stream_events=stream_events)
        case 'groups':
            return _export_groups(args)
        case 'dashboards':
            return _export_dashboards(args)
        case 'dashboard':
            return _export_dashboard(args)
        case _:
            parser.print_help(sys.stderr)
            return 2


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
            return _run_export_command(args, now, parser, fetch_events=fetch_events, stream_events=stream_events)
        case 'logs' | 'tail' | 'dash':
            return _run_shell_command(args, now, run_shell)
        case _ if interactive:
            return _run_shell_command(args, now, run_shell)
        case _:
            parser.print_help(sys.stderr)
            return 2
