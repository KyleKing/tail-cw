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
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import chain
from pathlib import Path

from beartype.typing import Protocol

from tail_cw.aws.client import LogEvent, fetch_log_events
from tail_cw.cache.storage import LogCache, generate_cache_key, read_parquet_to_log_events
from tail_cw.config import TailCWConfig, get_default_cache_dir, load_config

FetchEvents = Callable[..., Iterator[LogEvent]]

_DURATION_RE = re.compile(r'(\d+)([dhm])')


class SupportsWriteStr(Protocol):
    """Text sink accepting str writes (e.g. sys.stdout, io.StringIO)."""

    def write(self, text: str, /) -> int:
        """Write text and return the number of characters written."""
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


def build_parser() -> argparse.ArgumentParser:
    """Build the tail-cw argument parser with subcommands."""
    parser = argparse.ArgumentParser(
        prog='tail-cw',
        description='CloudWatch Logs fetcher and TUI viewer with local Parquet caching.',
    )
    subparsers = parser.add_subparsers(dest='command')

    fetch = subparsers.add_parser(
        'fetch',
        help='Fetch log events for a time range and open the TUI (or emit NDJSON).',
    )
    fetch.add_argument('log_group', help='CloudWatch log group name (e.g. /aws/lambda/my-function)')
    fetch.add_argument('--config', dest='config_path', type=Path, default=None, help='Config file path override')
    fetch.add_argument('--end', default=None, help='End of range: duration (2h) or ISO-8601 datetime (default: now)')
    fetch.add_argument('--filter', dest='filter_pattern', default=None, help='CloudWatch Logs filter pattern')
    fetch.add_argument(
        '--json',
        dest='json_output',
        action='store_true',
        help='Write events as NDJSON to stdout instead of opening the TUI',
    )
    fetch.add_argument(
        '--no-cache',
        dest='no_cache',
        action='store_true',
        help='Bypass the cache read (results are still written to the cache)',
    )
    fetch.add_argument('--profile', default=None, help='AWS profile name')
    fetch.add_argument('--region', default=None, help='AWS region name')
    fetch.add_argument(
        '--start',
        default='1h',
        help='Start of range: duration (15m, 2h, 3d) or ISO-8601 datetime (default: 1h)',
    )
    return parser


def _fetch_request_from_args(args: argparse.Namespace, now: datetime) -> FetchRequest:
    start_time = parse_time(args.start, now=now)
    end_time = parse_time(args.end, now=now) if args.end is not None else now
    if start_time >= end_time:
        msg = f'--start ({start_time.isoformat()}) must be before --end ({end_time.isoformat()})'
        raise ValueError(msg)
    return FetchRequest(
        log_group=args.log_group,
        start_time=start_time,
        end_time=end_time,
        filter_pattern=args.filter_pattern,
        profile=args.profile,
        region=args.region,
    )


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
    effective_fetch = fetch_events if fetch_events is not None else fetch_log_events
    cache_key = generate_cache_key(
        request.log_group,
        request.start_time,
        request.end_time,
        filter_pattern=request.filter_pattern,
        region_name=request.region,
        profile_name=request.profile,
    )
    cache_dir = request_cache_dir(config)
    with LogCache(
        cache_dir,
        size_limit_mb=config.cache.size_limit_mb,
        default_ttl_seconds=config.cache.default_ttl_seconds,
        eviction_policy=config.cache.eviction_policy,
        compression_level=config.parquet.compression_level,
        row_group_size=config.parquet.row_group_size,
        infer_schema_length=config.parquet.infer_schema_length,
    ) as cache:
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


def request_cache_dir(config: TailCWConfig) -> Path:
    """Return the configured cache directory, falling back to the XDG default."""
    return config.cache.cache_dir if config.cache.cache_dir is not None else get_default_cache_dir()


def write_ndjson(events: Iterable[LogEvent], stream: SupportsWriteStr) -> int:
    """Write log events to a stream as NDJSON and return the number written."""
    count = 0
    for event in events:
        record = {
            'timestamp': event.timestamp.isoformat(),
            'log_group': event.log_group,
            'log_stream': event.log_stream,
            'message': event.message,
            'event_id': event.event_id,
        }
        stream.write(json.dumps(record, separators=(',', ':')) + '\n')
        count += 1
    return count


RunTui = Callable[[TailCWConfig, Path, FetchRequest], None]


def run_cli(
    argv: Sequence[str] | None,
    run_tui: RunTui,
    *,
    fetch_events: FetchEvents | None = None,
) -> int:
    """Parse arguments and dispatch to the requested subcommand.

    Returns the process exit code.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help(sys.stderr)
        return 2

    now = datetime.now(tz=UTC)
    try:
        request = _fetch_request_from_args(args, now)
    except ValueError as err:
        sys.stderr.write(f'{err}\n')
        return 2

    try:
        config = load_config(args.config_path)
    except (OSError, ValueError) as err:
        sys.stderr.write(f'Configuration error: {err}\n')
        return 1

    parquet_path = resolve_parquet_path(
        request,
        config,
        use_cache=not args.no_cache,
        fetch_events=fetch_events,
    )
    if parquet_path is None:
        sys.stderr.write('No events found for the requested range\n')
        return 0

    if args.json_output:
        write_ndjson(read_parquet_to_log_events(parquet_path), sys.stdout)
        return 0

    run_tui(config, parquet_path, request)
    return 0
