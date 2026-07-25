"""The group browser: the shell's home view.

Answers "which of these forty groups holds the request I want?" before any
query is committed. The list is the M2 resolution ladder applied live, and the
pane on the right samples the highlighted group and shows its recurring message
shapes with counts, per ADR 0008.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, ClassVar

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable, Input

from tail_cw.aws.log_groups import LogGroupInfo, resolve_group_pattern
from tail_cw.preview import GroupPreview
from tail_cw.tui.picker import (
    DEBOUNCE_SECONDS,
    SELECTED_MARKER,
    Debounce,
    FilterInput,
    Picker,
    PickerColumn,
    format_created,
    format_retention,
    format_window,
    humanize_bytes,
    selection_status,
)
from tail_cw.tui.shell import MAX_SELECTED_GROUPS, ShellCommand, ShellScreen

if TYPE_CHECKING:
    from tail_cw.tui.navigation import NavTarget

NO_GROUP_SERVICE = 'No log group listing available. Start tail-cw with AWS credentials to browse groups.'
"""Shown in place of the list when no listing service is wired."""

NO_PREVIEW_SERVICE = 'No preview available. Start tail-cw with AWS credentials to sample a group.'
"""Shown in place of a preview when no preview service is wired."""

_COLUMNS = (
    PickerColumn(key='marker', label=' ', width=2),
    PickerColumn(key='name', label='Log group'),
    PickerColumn(key='stored', label='Stored', width=10),
    PickerColumn(key='retention', label='Retain', width=7),
    PickerColumn(key='created', label='Created', width=11),
)


def render_preview(preview: GroupPreview) -> Text:
    """Render a group preview as the ADR's header line plus one line per shape."""
    window = format_window(preview.window_seconds)
    body = Text()
    body.append(preview.log_group, style='bold')
    body.append(f'\n{preview.event_count} events, last {window}\n', style='dim')
    if not preview.patterns:
        body.append('\nNo events in this window', style='dim italic')
        return body
    for pattern in preview.patterns:
        label = pattern.key or pattern.example
        body.append(f'\n{pattern.count:>6}  ')
        body.append(label)
    return body


class GroupsScreen(ShellScreen):
    """Browse log groups, preview their contents, and open a selection."""

    BINDINGS: ClassVar[Sequence[Binding]] = [
        Binding('slash', 'open_filter', 'Filter', key_display='/'),
        Binding('space', 'toggle_select', 'Select'),
        Binding('enter', 'open_logs', 'Open logs'),
        Binding('t', 'open_tail', 'Live tail'),
        Binding('c', 'clear_selection', 'Clear selection'),
        Binding('r', 'reload', 'Reload'),
    ]

    def __init__(self, *, debounce_seconds: float = DEBOUNCE_SECONDS) -> None:
        """Start empty; everything comes off the shell once mounted.

        ``debounce_seconds`` exists so tests can make the preview timer
        deterministic; the shell always takes the default.
        """
        super().__init__()
        self._debounce_seconds = debounce_seconds
        self._groups: list[LogGroupInfo] = []
        self._visible: list[LogGroupInfo] = []
        self._selected: list[str] = []
        self._previews: dict[str, GroupPreview] = {}
        self._preview_debounce: Debounce | None = None

    def compose_content(self) -> ComposeResult:  # noqa: PLR6301
        """Yield the list-and-preview picker.

        Yields:
            One Picker over the log group columns.
        """
        yield Picker(_COLUMNS, placeholder='/ filter groups (Enter keeps, Esc clears)')

    def on_mount(self) -> None:
        """Draw the breadcrumb, adopt the session selection, and load the list."""
        super().on_mount()
        self._preview_debounce = Debounce(self, self._debounce_seconds)
        self._selected = list(self.shell.session.selected_groups)[:MAX_SELECTED_GROUPS]
        self._update_status()
        self.restore_focus()
        self._load_groups()

    def commands(self) -> dict[str, ShellCommand]:  # noqa: PLR6301
        """Add the group-browser commands to the shared set."""
        return {
            'clear': ShellCommand('Clear the log group selection'),
            'reload': ShellCommand('Re-read the log group list'),
            'select': ShellCommand('Select every group matching a pattern', ('<group>',)),
        }

    def run_view_command(self, name: str, argument: str) -> bool:
        """Handle the group-browser commands, reporting whether one matched."""
        match name:
            case 'clear':
                self.action_clear_selection()
            case 'reload':
                self.action_reload()
            case 'select':
                self._select_pattern(argument)
            case _:
                return False
        return True

    def nav_siblings(self) -> list[NavTarget]:  # noqa: PLR6301
        """The group browser has no level to cycle through."""
        return []

    def refresh_view(self) -> None:
        """Re-sample previews, since the preview window follows the session."""
        self._previews.clear()
        self._update_status()
        self._request_preview()

    def restore_focus(self) -> None:
        """Put the cursor back on the table so the view's keys work."""
        self._picker.table.focus()

    @property
    def _picker(self) -> Picker:
        return self.query_one(Picker)

    @property
    def selected_groups(self) -> list[str]:
        """The multi-selected group names, in selection order."""
        return list(self._selected)

    @property
    def visible_groups(self) -> list[str]:
        """The group names the current filter leaves visible."""
        return [info.name for info in self._visible]

    def highlighted_group(self) -> str | None:
        """The group under the cursor, or None when the list is empty."""
        row = self._picker.table.cursor_row
        if 0 <= row < len(self._visible):
            return self._visible[row].name
        return None

    def action_open_filter(self) -> None:
        """Open the ``/`` filter box."""
        self._picker.open_filter()

    def action_toggle_select(self) -> None:
        """Add or remove the highlighted group, refusing past the live-tail cap."""
        name = self.highlighted_group()
        if name is None:
            return
        if name in self._selected:
            self._selected.remove(name)
        elif len(self._selected) >= MAX_SELECTED_GROUPS:
            self.notify(f'At most {MAX_SELECTED_GROUPS} groups; deselect one first', severity='warning')
            return
        else:
            self._selected.append(name)
        self.shell.session.selected_groups = list(self._selected)
        self._mark_row(name)
        self._update_status()

    def action_clear_selection(self) -> None:
        """Drop every selected group."""
        cleared = self._selected
        self._selected = []
        self.shell.session.selected_groups = []
        for name in cleared:
            self._mark_row(name)
        self._update_status()

    def action_open_logs(self) -> None:
        """Open the historical log view over the selection."""
        self._open(live=False)

    def action_open_tail(self) -> None:
        """Stream the selection live."""
        self._open(live=True)

    def action_reload(self) -> None:
        """Re-read the group list from AWS."""
        self._groups = []
        self._load_groups()

    @on(DataTable.RowHighlighted, '#picker_table')
    def _on_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        event.stop()
        self._request_preview()

    @on(DataTable.RowSelected, '#picker_table')
    def _on_row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        self.action_open_logs()

    @on(Input.Changed, '#picker_filter')
    def _on_filter_changed(self, event: Input.Changed) -> None:
        event.stop()
        self._apply_filter(event.value)

    @on(Input.Submitted, '#picker_filter')
    def _on_filter_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._picker.filter_input.display = False
        self.restore_focus()

    @on(FilterInput.Cancelled)
    def _on_filter_cancelled(self, event: FilterInput.Cancelled) -> None:
        event.stop()
        self._picker.filter_input.value = ''
        self._picker.close_filter()

    def _open(self, *, live: bool) -> None:
        highlighted = self.highlighted_group()
        groups = self._selected or ([highlighted] if highlighted is not None else [])
        if not groups:
            self.notify('No log group to open', severity='warning')
            return
        self.shell.open_logs(groups, live=live)

    def _select_pattern(self, argument: str) -> None:
        matched = [info.name for info in resolve_group_pattern(argument, self._groups)] if argument else []
        if not matched:
            self.notify(f'No group matches {argument!r}', severity='warning')
            return
        room = MAX_SELECTED_GROUPS - len(self._selected)
        added = [name for name in matched if name not in self._selected][:room]
        if not added:
            self.notify(f'At most {MAX_SELECTED_GROUPS} groups; deselect one first', severity='warning')
            return
        self._selected.extend(added)
        self.shell.session.selected_groups = list(self._selected)
        for name in added:
            self._mark_row(name)
        self._update_status()

    def _load_groups(self) -> None:
        if self.shell.services.list_groups is None:
            self._picker.show_detail(NO_GROUP_SERVICE)
            self._picker.set_status(NO_GROUP_SERVICE)
            return
        self.run_worker(self._fetch_groups, name='list_groups', group='list_groups', exclusive=True, thread=True)

    def _fetch_groups(self) -> None:
        list_groups = self.shell.services.list_groups
        if list_groups is None:  # pragma: no cover - guarded by the caller
            return
        try:
            groups = list_groups()
        except Exception as err:
            self.app.call_from_thread(self.notify, f'Listing log groups failed: {err}', severity='error')
            return
        self.app.call_from_thread(self._apply_groups, groups)

    def _apply_groups(self, groups: list[LogGroupInfo]) -> None:
        self._groups = groups
        self.shell.session.group_names = [info.name for info in groups]
        self._apply_filter(self._picker.filter_value)

    def _apply_filter(self, pattern: str) -> None:
        self._visible = resolve_group_pattern(pattern, self._groups)
        table = self._picker.table
        table.clear()
        for info in self._visible:
            table.add_row(*self._row_cells(info), key=info.name)
        self._update_status()
        self._request_preview()

    def _row_cells(self, info: LogGroupInfo) -> tuple[str, str, str, str, str]:
        marker = SELECTED_MARKER if info.name in self._selected else ''
        return (
            marker,
            info.name,
            humanize_bytes(info.stored_bytes),
            format_retention(info.retention_days),
            format_created(info.created),
        )

    def _mark_row(self, name: str) -> None:
        if name not in self.visible_groups:
            return
        marker = SELECTED_MARKER if name in self._selected else ''
        self._picker.table.update_cell(name, 'marker', marker)

    def _update_status(self) -> None:
        self._picker.set_status(
            selection_status(
                visible=len(self._visible),
                total=len(self._groups),
                selected=len(self._selected),
                cap=MAX_SELECTED_GROUPS,
            )
        )

    def _request_preview(self) -> None:
        name = self.highlighted_group()
        if name is None:
            if self._groups:
                self._picker.show_detail('No group matches this filter')
            return
        if self.shell.services.preview_group is None:
            self._picker.show_detail(NO_PREVIEW_SERVICE)
            return
        cached = self._previews.get(name)
        if cached is not None:
            self._picker.show_detail(render_preview(cached))
            return
        self._picker.show_detail(f'Sampling {name}...')
        if self._preview_debounce is not None:
            self._preview_debounce.schedule(lambda: self._start_preview(name))

    def _start_preview(self, name: str) -> None:
        self.run_worker(
            lambda: self._fetch_preview(name),
            name='preview_group',
            group='preview_group',
            exclusive=True,
            thread=True,
        )

    def _fetch_preview(self, name: str) -> None:
        preview_group = self.shell.services.preview_group
        if preview_group is None:  # pragma: no cover - guarded by the caller
            return
        try:
            preview = preview_group(name)
        except Exception as err:
            self.app.call_from_thread(self.notify, f'Preview of {name} failed: {err}', severity='warning')
            return
        self.app.call_from_thread(self._apply_preview, preview)

    def _apply_preview(self, preview: GroupPreview) -> None:
        self._previews[preview.log_group] = preview
        if self.highlighted_group() == preview.log_group:
            self._picker.show_detail(render_preview(preview))
