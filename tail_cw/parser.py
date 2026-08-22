"""The argparse surface, and nothing that a parse needs to load.

``tail-cw --help`` and a rejected typo both exit inside ``parse_args``, so this
module deliberately imports no AWS client, no Polars, and no Textual: the entry
point builds the parser from here and reaches for the pipelines only once a
command is chosen. Measured 2026-08-22, importing the pipelines cost 278ms
against 0.03s for a bare interpreter.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from tail_cw.aws.alarms import ALARM_STATES
from tail_cw.aws.insights import MAX_INSIGHTS_LOG_GROUPS
from tail_cw.query.fuzzy import DEFAULT_SIMILARITY
from tail_cw.query.rollup import DEFAULT_PATTERN_LIMIT, Granularity
from tail_cw.query.severity import Severity

DEFAULT_WINDOW = '1h'
DEFAULT_DASHBOARD_WINDOW = '3h'
DEFAULT_SUMMARY_MAX_GROUPS = 25
INSIGHTS_DEFAULT_LIMIT = 1000
DEFAULT_HISTORY_WINDOW = '7d'


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
    _configure_trace(
        export_sub.add_parser('trace', help='Write one trace as OTLP JSON, for a viewer that draws waterfalls.'),
    )
    _configure_xray(
        export_sub.add_parser('xray', help='Write X-Ray trace summaries for a time range as NDJSON.'),
    )
    _configure_xray_trace(
        export_sub.add_parser('xray-trace', help='Write full X-Ray traces as OTLP JSON, with real span timings.'),
    )
    _configure_alarms(export_sub.add_parser('alarms', help='Write metric alarms, and their firing history, as NDJSON.'))
    _configure_metrics(export_sub.add_parser('metrics', help='Write metric datapoints as NDJSON.'))
    _configure_dimensions(
        export_sub.add_parser('dimensions', help='Write the dimension sets a namespace publishes as NDJSON.'),
    )
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


def _configure_trace(parser: argparse.ArgumentParser) -> None:
    parser.add_argument('trace_id', help='Trace identifier to collect spans for')
    parser.add_argument('patterns', nargs='*', help='Log group names or glob patterns (omit for every group)')
    _add_aws_flags(parser)
    _add_window_flags(parser, default_start=DEFAULT_WINDOW)
    parser.add_argument(
        '--max-groups',
        type=int,
        default=DEFAULT_SUMMARY_MAX_GROUPS,
        help=f'Cap on groups read (default: {DEFAULT_SUMMARY_MAX_GROUPS})',
    )
    parser.add_argument('--no-cache', action='store_true', help='Bypass the cache read')


def _configure_xray(parser: argparse.ArgumentParser) -> None:
    _add_aws_flags(parser)
    parser.add_argument(
        '--start',
        default=DEFAULT_WINDOW,
        help=f'Start of range: duration (15m, 2h, 3d) or ISO-8601 datetime (default: {DEFAULT_WINDOW})',
    )
    parser.add_argument('--end', default=None, help='End of range: duration or ISO-8601 datetime')
    parser.add_argument(
        '--expression',
        dest='filter_expression',
        default=None,
        help='X-Ray filter expression applied server-side, e.g. \'service("api") AND responsetime > 3\'',
    )
    parser.add_argument(
        '--sampling',
        action='store_true',
        help='Ask X-Ray for a representative sample rather than every trace',
    )
    parser.add_argument('--limit', type=int, default=None, help='Stop after this many traces')


def _configure_xray_trace(parser: argparse.ArgumentParser) -> None:
    parser.add_argument('trace_ids', nargs='+', help='One or more X-Ray trace ids (1-<hex>-<hex>)')
    _add_aws_flags(parser)


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


def _configure_dimensions(parser: argparse.ArgumentParser) -> None:
    parser.add_argument('--namespace', required=True, help='Metric namespace, e.g. AWS/ECS')
    parser.add_argument('--metric', default=None, help='Restrict to one metric name')
    _add_aws_flags(parser)


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
