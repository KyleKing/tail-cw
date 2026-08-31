"""Turn log events into the cells the log table shows.

Two decisions live here. The column budget is spent by priority rather than on
fixed widths, because at 80 columns four fixed columns left Message twelve
characters wide. And a JSON record is read the way a person reads it: the phrase
it carries first, then its remaining fields as ``key=value`` pairs, rather than
as a wall of braces that clips before the interesting part.

Severity is shown twice over, as a glyph and as colour, so it survives
``NO_COLOR``. A record that declares its own level is coloured strongly; one
classified by reading its prose is dimmed, because a confident wrong colour in a
thousand-row table is worse than a hedged right one.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from rich.console import RenderableType
from rich.text import Text

from tail_cw.aws.events import LogEvent
from tail_cw.cache.records import is_jsonl_message, strip_timestamp_prefix
from tail_cw.config import MessageConfig
from tail_cw.query.severity import Classification, Severity, classify_event, load_json_dict
from tail_cw.text import shorten

FULL_TIME_WIDTH = 23
COMPACT_TIME_WIDTH = 12
SECONDS_TIME_WIDTH = 8
SEVERITY_WIDTH = 1
GROUP_WIDTH = 20
STREAM_WIDTH = 16
MIN_MESSAGE_WIDTH = 24

DATE_ONLY_ABOVE = 100
"""Terminal width at which a window spanning more than one day may show the date.

A single-day window never shows it at any width: every row shares the window the
breadcrumb already states, so the date repeats a thousand times to no purpose and
takes eleven columns from Message to do it.
"""

DROP_STREAM_BELOW = 120
"""Terminal width under which the stream column goes; the detail pane has it in full."""

DROP_MILLIS_BELOW = 80
"""Terminal width under which the sub-second digits go.

The last rung of the ladder. Milliseconds separate two events in the same second, which
matters far less than the four characters of message they cost on a narrow terminal.
"""

SEVERITY_GLYPHS = {Severity.ERROR: '✖', Severity.WARNING: '⚠', Severity.INFO: ' '}
_EXPLICIT_STYLES = {Severity.ERROR: 'bold red', Severity.WARNING: 'bold yellow', Severity.INFO: ''}
_INFERRED_STYLES = {Severity.ERROR: 'dim red', Severity.WARNING: 'dim yellow', Severity.INFO: ''}
_PAIR_STYLE = 'dim'
_PAIR_STYLES = {Severity.ERROR: '', Severity.WARNING: '', Severity.INFO: _PAIR_STYLE}
"""Style for the ``key=value`` remainder, which is where a status code and a latency live.

Dim on an ordinary row, plain text on one that matters. Dimming an error row's detail put
it at 1.9:1, and colouring it the theme's red only reaches 2.7:1, against 7.7:1 for plain
text: the glyph and the coloured phrase already say the row is an error, so the detail is
free to be the most readable thing it can be.
"""


@dataclass(frozen=True)
class Column:
    """One table column and the width it was given.

    Attributes:
        key: Stable identifier, used as the DataTable column key.
        label: Header text.
        width: Fixed width in cells.
    """

    key: str
    label: str
    width: int


def plan_columns(width: int, *, single_group: bool, multi_day: bool = False) -> tuple[Column, ...]:
    """Choose the columns a table of this width can afford.

    Args:
        width: Cells available to the table.
        single_group: True when every row shares one log group, which makes the
            group column a constant repeated on every row.
        multi_day: True when the window spans more than one calendar day, which
            is the only case where the date tells a reader something the
            breadcrumb does not.
    """
    if multi_day and width >= DATE_ONLY_ABOVE:
        time_width = FULL_TIME_WIDTH
    elif width >= DROP_MILLIS_BELOW:
        time_width = COMPACT_TIME_WIDTH
    else:
        time_width = SECONDS_TIME_WIDTH
    columns = [
        # A header wider than its column renders clipped, which reads as a rendering bug.
        Column('timestamp', 'Timestamp' if time_width >= len('Timestamp') else 'Time', time_width),
        Column('severity', '!', SEVERITY_WIDTH),
    ]
    if not single_group:
        columns.append(Column('log_group', 'Log Group', GROUP_WIDTH))
    if width >= DROP_STREAM_BELOW:
        columns.append(Column('log_stream', 'Log Stream', STREAM_WIDTH))
    spent = sum(column.width + 2 for column in columns)
    return (*columns, Column('message', 'Message', max(width - spent - 2, MIN_MESSAGE_WIDTH)))


def format_timestamp(moment: datetime, *, width: int = FULL_TIME_WIDTH, style: str = 'cyan') -> Text:
    """Render an event time, shedding the date then the sub-second digits as room runs out."""
    if width >= FULL_TIME_WIDTH:
        return Text(moment.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3], style=style)
    if width >= COMPACT_TIME_WIDTH:
        return Text(moment.strftime('%H:%M:%S.%f')[:-3], style=style)
    return Text(moment.strftime('%H:%M:%S'), style=style)


def format_row(
    event: LogEvent,
    columns: Sequence[Column],
    config: MessageConfig,
) -> tuple[RenderableType, ...]:
    """Render one event as cells matching ``columns``."""
    classification = classify_event(event)
    cells: dict[str, RenderableType] = {
        'timestamp': format_timestamp(event.timestamp, width=_width_of(columns, 'timestamp')),
        'severity': Text(SEVERITY_GLYPHS[classification.severity], style=_style_for(classification)),
        'log_group': shorten(event.log_group, _width_of(columns, 'log_group')),
        'log_stream': shorten(event.log_stream, _width_of(columns, 'log_stream')),
        'message': message_text(event, config, classification, width=_width_of(columns, 'message')),
    }
    return tuple(cells[column.key] for column in columns)


def format_rows(
    events: Iterable[LogEvent],
    columns: Sequence[Column],
    config: MessageConfig,
) -> list[tuple[RenderableType, ...]]:
    """Render many events at once, which is how the table is filled."""
    return [format_row(event, columns, config) for event in events]


def message_text(
    event: LogEvent,
    config: MessageConfig,
    classification: Classification | None = None,
    *,
    width: int = 0,
) -> Text:
    """Render an event body as its phrase, then its remaining fields.

    A record's own phrase (``event``, ``message``, ``msg`` by default) reads as a
    sentence, so it leads and carries the severity colour. Everything else
    follows as ``key=value``, which keeps the fields available without letting
    braces and quotes eat the column.
    """
    hint = classification if classification is not None else classify_event(event)
    style = _style_for(hint)
    data = load_json_dict(event.message)
    if data is None:
        text = Text(strip_timestamp_prefix(event.message), style=style)
    else:
        text = _structured_text(data, config, style=style, pair_style=_PAIR_STYLES[hint.severity])
    if width:
        text.truncate(width, overflow='ellipsis')
    return text


def _structured_text(data: Mapping[str, Any], config: MessageConfig, *, style: str, pair_style: str) -> Text:
    remainder = {key: value for key, value in data.items() if key not in config.hidden_fields}
    phrase = ''
    for candidate in config.phrase_fields:
        value = remainder.get(candidate)
        if isinstance(value, str) and value:
            phrase = value
            del remainder[candidate]
            break
    # The severity style goes on the phrase as a span, not on the Text: a base style
    # applies to every append too, so the remainder inherited the error colour and no
    # per-pair style could give it back.
    text = Text()
    text.append(phrase, style=style)
    for key, value in remainder.items():
        if value is None:
            continue
        text.append(f'{" " if text.plain else ""}{key}={_render_value(value)}', style=pair_style)
    return text


def _render_value(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(',', ':'))
    return str(value)


def _style_for(classification: Classification) -> str:
    styles = _EXPLICIT_STYLES if classification.explicit else _INFERRED_STYLES
    return styles[classification.severity]


def _width_of(columns: Sequence[Column], key: str) -> int:
    return next((column.width for column in columns if column.key == key), 0)


def format_log_event_detail(event: LogEvent) -> str:
    """Render every field of one event, with its message in full, for the detail pane."""
    ingestion = event.ingestion_time.isoformat() if event.ingestion_time else 'N/A'
    return (
        f'Timestamp: {event.timestamp.isoformat()}\n'
        f'Log Group: {event.log_group}\n'
        f'Log Stream: {event.log_stream}\n'
        f'Ingestion Time: {ingestion}\n'
        f'\nMessage:\n{event.message}'
    )


def parse_jsonl_message(message: str) -> str | None:
    """Pretty-print a JSON message, or return None when it is not JSON."""
    if not is_jsonl_message(message):
        return None
    try:
        return json.dumps(json.loads(message), indent=2, sort_keys=True)
    except (json.JSONDecodeError, ValueError):
        return None


def format_log_event_detail_with_json(event: LogEvent) -> str:
    """Render one event for the detail pane, adding pretty-printed JSON when it parses."""
    detail = format_log_event_detail(event)
    parsed = parse_jsonl_message(event.message)
    if parsed is None:
        return detail
    header, _, _ = detail.partition('Message:\n')
    return f'{header}Message (raw):\n{event.message}\n\nMessage (parsed JSON):\n{parsed}'
