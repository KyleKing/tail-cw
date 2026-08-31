"""Render a rollup as markdown for a human or an issue tracker.

Pure formatting: takes a :class:`~tail_cw.query.rollup.RollupReport` and returns text.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from tail_cw.aws.alarms import AlarmSummary
from tail_cw.charts.sparkline import sparkline_blocks
from tail_cw.query.facets import FieldFacet
from tail_cw.query.rollup import Granularity, PatternRollup, RollupReport
from tail_cw.text import shorten

_SPARK_WIDTH = 24
_SUMMARY_EXAMPLE_CHARS = 110
_FACET_VALUE_CHARS = 60


def render_markdown(report: RollupReport, *, title: str, window_label: str, source: str) -> str:
    """Render the ranked patterns as a markdown document."""
    lines = [f'# {title}', '', f'{window_label} · {source}', '', _totals_sentence(report), '']
    if not report.patterns:
        lines.append('No events at or above the requested severity.')
        return '\n'.join(lines) + '\n'

    lines.extend(_summary_table(report))
    lines.extend(['', '## Patterns', ''])
    for index, pattern in enumerate(report.patterns, start=1):
        lines.extend(_pattern_section(index, pattern, report))
    return '\n'.join(lines) + '\n'


def render_alarm_markdown(alarms: Sequence[AlarmSummary], transitions: Mapping[str, int]) -> str:
    """Render alarms as a markdown table, most-changed first.

    Ranking by transition count is the view that answers "which alarm is
    flapping", which is the question a list ordered by name cannot answer.
    """
    ranked = sorted(alarms, key=lambda alarm: (-transitions.get(alarm.name, 0), alarm.name))
    columns = ('alarm', 'state', 'transitions', 'metric', 'reason')
    rows = [
        {
            'alarm': shorten(alarm.name, 44),
            'state': alarm.state,
            'transitions': str(transitions.get(alarm.name, '-')),
            'metric': shorten(f'{alarm.namespace or "-"}/{alarm.metric_name or "math"}', 36),
            'reason': shorten(alarm.state_reason, 60),
        }
        for alarm in ranked
    ]
    return render_rows_markdown(columns, rows)


def render_rows_markdown(columns: Sequence[str], rows: Sequence[Mapping[str, str]]) -> str:
    """Render arbitrary query rows as a markdown table."""
    if not columns:
        return 'No rows returned.\n'
    header = '| ' + ' | '.join(columns) + ' |'
    divider = '|' + '|'.join(['---'] * len(columns)) + '|'
    body = ['| ' + ' | '.join(_escape_cell(row.get(column, '')) for column in columns) + ' |' for row in rows]
    return '\n'.join([header, divider, *body]) + '\n'


def render_facets_markdown(facets: Sequence[FieldFacet], *, window_label: str) -> str:
    """Render field counts as one markdown section per field, widest field first."""
    lines = ['# Field counts', '', window_label, '']
    for facet in facets:
        share = f'{facet.present:,} records, {facet.distinct:,} distinct'
        if facet.truncated:
            share += f', top {len(facet.values)} shown'
        lines.extend([f'## `{facet.path}`', '', share, ''])
        if facet.values:
            lines.extend(
                render_rows_markdown(
                    ('value', 'count', 'share'),
                    [
                        {
                            'value': _inline_code(shorten(value.value, _FACET_VALUE_CHARS)),
                            'count': f'{value.count:,}',
                            'share': f'{value.count / facet.present:.1%}' if facet.present else '-',
                        }
                        for value in facet.values
                    ],
                ).splitlines()
            )
        lines.append('')
    return '\n'.join(lines) + '\n'


def _escape_cell(value: str) -> str:
    return value.replace('|', '\\|')


def _totals_sentence(report: RollupReport) -> str:
    counts = ', '.join(f'{count:,} {severity.name.lower()}' for severity, count in report.severity_totals)
    matched = f'{report.matched:,} matched ({counts})' if counts else '0 matched'
    shapes = f'{report.distinct_shapes:,} distinct shapes'
    if report.distinct_patterns != report.distinct_shapes:
        shapes += f', merged to {report.distinct_patterns:,} patterns'
    hidden = report.distinct_patterns - len(report.patterns)
    tail = f' Top {len(report.patterns)} shown, {hidden:,} not listed.' if hidden > 0 else ''
    return f'Scanned {report.scanned:,} events, {matched} in {shapes}.{tail}'


def _summary_table(report: RollupReport) -> list[str]:
    period = 'hourly' if report.granularity is Granularity.HOUR else 'daily'
    rows = [
        f'| # | Severity | Count | {period.capitalize()} trend | Log groups | Shape |',
        '|--:|---|--:|---|---|---|',
    ]
    for index, pattern in enumerate(report.patterns, start=1):
        groups = ', '.join(f'{name} ({count})' for name, count in pattern.log_groups)
        shape = _inline_code(shorten(pattern.key, _SUMMARY_EXAMPLE_CHARS))
        count = f'{pattern.count:,}' + (f' ({pattern.merged_shapes} shapes)' if pattern.merged_shapes > 1 else '')
        rows.append(
            f'| {index} | {pattern.severity.name.lower()} | {count} '
            f'| `{_trend(pattern, report)}` | {groups} | {shape} |',
        )
    return rows


def _pattern_section(index: int, pattern: PatternRollup, report: RollupReport) -> list[str]:
    counts = ', '.join(f'{label} {count}' for label, count in _bucket_series(pattern, report))
    return [
        f'### {index}. {pattern.severity.name.lower()}, {pattern.count:,} events',
        '',
        f'First {pattern.first_seen.isoformat()}, last {pattern.last_seen.isoformat()}.',
        '',
        f'Per {report.granularity.value}: {counts}',
        '',
        'Example:',
        '',
        '```',
        pattern.example,
        '```',
        '',
        'Normalized shape:',
        '',
        '```',
        pattern.key,
        '```',
        '',
    ]


def _bucket_series(pattern: PatternRollup, report: RollupReport) -> list[tuple[str, int]]:
    counts = dict(pattern.buckets)
    labels = report.bucket_labels or tuple(counts)
    return [(label, counts.get(label, 0)) for label in labels]


def _trend(pattern: PatternRollup, report: RollupReport) -> str:
    values = [float(count) for _, count in _bucket_series(pattern, report)]
    return sparkline_blocks(values, width=_SPARK_WIDTH, bars=True, lo=0.0) or '-'


def _inline_code(text: str) -> str:
    return '`' + text.replace('`', "'").replace('|', '\\|') + '`'
