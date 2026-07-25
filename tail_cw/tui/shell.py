"""The single Textual application that hosts every tail-cw view.

One app owns the navigation stack, the shared session (time window, filter,
profile, selected log groups), and the ``:`` command line; the views are
screens over that state. Views never reach AWS directly. Every network call
arrives as a callable on ``ShellServices``, which ``tail_cw.__main__`` fills
with the real pipelines, ``tail_cw.demo`` with generated data, and tests with
fakes, so the app itself stays importable without boto3 credentials.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Footer, Input, Label

from tail_cw.aws.client import LogEvent
from tail_cw.aws.dashboards import Dashboard, DashboardSummary, DiveCandidate, Widget
from tail_cw.aws.log_groups import LogGroupInfo
from tail_cw.aws.metrics import MetricSeries
from tail_cw.cli import Session, expand_presets
from tail_cw.config import TailCWConfig
from tail_cw.preview import GroupPreview
from tail_cw.recents import Recents, load_recents, profile_recents, record_selection, save_recents
from tail_cw.tui.command_bar import CommandLine
from tail_cw.tui.dive_screen import DiveConfirmScreen
from tail_cw.tui.navigation import (
    NavState,
    NavTarget,
    ViewKind,
    breadcrumb,
    current,
    initial,
    jump_back,
    jump_forward,
    pop,
    push,
    replace_top,
    sibling,
)
from tail_cw.tui.which_key import WhichKeyScreen

ListGroups = Callable[[], Awaitable[list[LogGroupInfo]]]
PreviewGroup = Callable[[str], Awaitable[GroupPreview]]
ListDashboards = Callable[[], Awaitable[list[DashboardSummary]]]
LoadDashboard = Callable[[str], Awaitable[Dashboard]]
FetchMetrics = Callable[[Sequence[dict[str, object]], datetime, datetime], Awaitable[list[MetricSeries]]]
ResolveLogs = Callable[[Sequence[str], datetime, datetime, str | None], Awaitable[list[Path]]]
LiveStream = Callable[[Sequence[str], str | None], AsyncIterator[LogEvent]]
LogVolume = Callable[[str, datetime, datetime], Awaitable[list[float]]]
CountEvents = Callable[[str, datetime, datetime], Awaitable[int]]
ScreenFactory = Callable[[NavTarget], 'ShellScreen']

MAX_SELECTED_GROUPS = 10

_RANGE_CHOICES = ('15m', '1h', '3h', '6h', '12h', '1d')
_DURATION_UNITS = {'m': 'minutes', 'h': 'hours', 'd': 'days'}
_MIN_DURATION_LENGTH = 2


@dataclass(frozen=True)
class ShellServices:
    """The AWS-facing callables the views depend on.

    Every field is optional so the app can run with no credentials; a view
    that needs a missing service says so rather than failing.
    """

    list_groups: ListGroups | None = None
    preview_group: PreviewGroup | None = None
    list_dashboards: ListDashboards | None = None
    load_dashboard: LoadDashboard | None = None
    fetch_metrics: FetchMetrics | None = None
    resolve_logs: ResolveLogs | None = None
    live_stream: LiveStream | None = None
    log_volume: LogVolume | None = None
    count_events: CountEvents | None = None


@dataclass(frozen=True)
class ShellCommand:
    """One ``:`` command: its summary and the argument values Tab offers."""

    summary: str
    args: tuple[str, ...] = ()


def parse_duration(text: str) -> timedelta | None:
    """Parse ``15m``, ``2h``, or ``3d`` into a timedelta, or None when malformed."""
    stripped = text.strip()
    if len(stripped) < _MIN_DURATION_LENGTH or not stripped[:-1].isdigit() or stripped[-1] not in _DURATION_UNITS:
        return None
    return timedelta(**{_DURATION_UNITS[stripped[-1]]: int(stripped[:-1])})


def _global_commands() -> dict[str, ShellCommand]:
    return {
        'dash': ShellCommand('Open a dashboard by name', ('<dashboard>',)),
        'dashboards': ShellCommand('List the dashboards in this account'),
        'filter': ShellCommand('Set the shared filter pattern; empty clears it'),
        'groups': ShellCommand('Browse log groups'),
        'help': ShellCommand('List the available commands'),
        'logs': ShellCommand('Search logs in the selected groups', ('<group>',)),
        'quit': ShellCommand('Leave tail-cw'),
        'range': ShellCommand('Set the time window ending now', _RANGE_CHOICES),
        'tail': ShellCommand('Stream the selected groups live', ('<group>',)),
    }


class ShellScreen(Screen[None]):
    """Base view: a breadcrumb header, content, the ``:`` line, and a footer.

    Subclasses provide ``compose_content`` and may add their own bindings,
    ``:`` commands, and sibling list. Navigation keys live here so every view
    answers to the same motions.
    """

    DEFAULT_CSS = """
    ShellScreen {
        layout: vertical;
    }
    #breadcrumb {
        dock: top;
        height: 1;
        background: $panel;
        color: $text;
        padding: 0 1;
    }
    """

    BINDINGS: ClassVar[Sequence[Binding]] = [  # type: ignore[assignment]
        Binding('colon', 'command', 'Command', key_display=':'),
        Binding('escape', 'nav_pop', 'Back'),
        Binding('ctrl+o', 'jump_back', 'Jump back'),
        Binding('ctrl+i', 'jump_forward', 'Jump fwd'),
        Binding('left_square_bracket', 'sibling_prev', 'Prev', key_display='['),
        Binding('right_square_bracket', 'sibling_next', 'Next', key_display=']'),
        Binding('q', 'quit', 'Quit'),
        Binding('question_mark', 'which_key', 'Keys', key_display='?'),
        Binding('comma', 'which_key', 'Keys', show=False),
    ]

    def compose(self) -> ComposeResult:
        """Wrap the subclass content in the shared chrome.

        The breadcrumb replaces Textual's ``Header`` rather than sitting under
        it. One identity line avoids duplicating the view name, and a ``Header``
        per stacked screen races its own ``HeaderTitle`` when a push and a title
        change land in the same message flush.

        Yields:
            The breadcrumb, the subclass content, the command line, and the footer.
        """
        yield Label('', id='breadcrumb')
        yield from self.compose_content()
        yield CommandLine(completer=self.complete_command)
        yield Footer()

    def compose_content(self) -> ComposeResult:  # ruff: ignore[no-self-use]
        """Yield the widgets unique to this view."""
        return iter(())

    def commands(self) -> dict[str, ShellCommand]:  # ruff: ignore[no-self-use]
        """Return the ``:`` commands this view adds to the global set."""
        return {}

    def run_view_command(self, name: str, argument: str) -> bool:  # ruff: ignore[no-self-use]
        """Run a view-specific command, returning False when it is unknown."""
        del name, argument
        return False

    def nav_siblings(self) -> list[NavTarget]:  # ruff: ignore[no-self-use]
        """Return the targets ``[`` and ``]`` cycle at this level."""
        return []

    def refresh_view(self) -> None:
        """Re-read shared session state after the window or filter changed."""

    @property
    def shell(self) -> TailCWApp:
        """The hosting application."""
        app = self.app
        if not isinstance(app, TailCWApp):
            msg = 'ShellScreen requires a TailCWApp host'
            raise TypeError(msg)
        return app

    def on_mount(self) -> None:
        """Render the breadcrumb for this view."""
        self.update_breadcrumb()

    def update_breadcrumb(self) -> None:
        """Redraw the breadcrumb from the app's navigation state."""
        self.query_one('#breadcrumb', Label).update(self.shell.breadcrumb_text())

    def action_command(self) -> None:
        """Open the command line."""
        self.query_one(CommandLine).open()

    def action_nav_pop(self) -> None:
        """Go up one level."""
        self.shell.nav_pop()

    def action_jump_back(self) -> None:
        """Walk back through the jumplist."""
        self.shell.nav_jump(forward=False)

    def action_jump_forward(self) -> None:
        """Walk forward through the jumplist."""
        self.shell.nav_jump(forward=True)

    def action_sibling_prev(self) -> None:
        """Move to the previous sibling at this level."""
        self.shell.nav_sibling(self.nav_siblings(), offset=-1)

    def action_sibling_next(self) -> None:
        """Move to the next sibling at this level."""
        self.shell.nav_sibling(self.nav_siblings(), offset=1)

    def action_which_key(self) -> None:
        """Show every binding and command available here."""
        self.shell.show_which_key(self)

    def complete_command(self, value: str) -> list[str]:
        """Complete a command name, then its argument values."""
        registry = self.shell.command_registry(self)
        parts = value.split()
        if not parts or (len(parts) == 1 and not value.endswith(' ')):
            prefix = parts[0] if parts else ''
            return sorted(name for name in registry if name.startswith(prefix))
        command = registry.get(parts[0])
        if command is None:
            return []
        prefix = parts[1] if len(parts) > 1 else ''
        candidates = self.shell.argument_values(command.args)
        return [f'{parts[0]} {value}' for value in candidates if value.lower().startswith(prefix.lower())]

    @on(Input.Submitted, '#command_line')
    def _on_command_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        line = self.query_one(CommandLine)
        text = event.value.strip()
        line.close()
        if text:
            line.remember(text)
            self.shell.run_command(self, text)
        self.restore_focus()

    def restore_focus(self) -> None:
        """Return focus to the view's main widget after the command line closes."""


class TailCWApp(App[None]):
    """Hosts every view, owning navigation, session state, and the services."""

    CSS = """
    Screen {
        scrollbar-size: 0 0;
    }
    """

    def __init__(
        self,
        config: TailCWConfig,
        session: Session,
        *,
        build_screen: ScreenFactory,
        services: ShellServices | None = None,
        target: NavTarget | None = None,
        recents_path: Path | None = None,
    ) -> None:
        """Store the config, shared session, injected services, and opening view.

        ``build_screen`` maps a navigation target to a view. It is injected
        because the views subclass ``ShellScreen``; importing them here would
        make the module cycle. ``tail_cw.tui.views`` supplies the real one.
        ``recents_path`` overrides the XDG data file the group history is kept in.
        """
        super().__init__()
        self.title = 'tail-cw'
        self.config_data = config
        self.session = session
        self.services = services if services is not None else ShellServices()
        self._recents_path = recents_path
        self.recents: Recents = load_recents(recents_path)
        self._build_screen = build_screen
        self._opening = target if target is not None else NavTarget(kind=ViewKind.GROUPS, label='groups')
        self._nav = initial(self._opening)
        self._commands = _global_commands()

    def get_default_screen(self) -> Screen[None]:  # ruff: ignore[no-self-use]
        """Return an empty base screen that the opening view is pushed over.

        The real root sits one level above Textual's default screen so the
        Textual stack is always deeper than one. ``switch_screen`` empties the
        stack when the default screen is the only entry, which is what ``[``
        and ``]`` would hit on the home view.
        """
        return Screen()

    def on_mount(self) -> None:
        """Push the opening view over the base screen."""
        self.push_screen(self.build_screen(self._opening))

    def build_screen(self, target: NavTarget) -> ShellScreen:
        """Construct the view for a navigation target."""
        return self._build_screen(target)

    def breadcrumb_text(self) -> str:
        """The current navigation path, followed by the shared window."""
        return f'tail-cw  ·  {breadcrumb(self._nav)}  ·  {self.session.window_label()}'

    @property
    def nav(self) -> NavState:
        """The current navigation state."""
        return self._nav

    def goto(self, target: NavTarget) -> None:
        """Push a view onto the stack and record it in the jumplist."""
        self._nav = push(self._nav, target)
        self.push_screen(self.build_screen(target))

    def nav_pop(self) -> None:
        """Go up one level, leaving the jumplist intact."""
        popped = pop(self._nav)
        if popped == self._nav:
            return
        self._nav = popped
        self.pop_screen()
        self._sync_breadcrumb()

    def nav_jump(self, *, forward: bool) -> None:
        """Walk the jumplist, replacing the current view with the recorded one."""
        moved = jump_forward(self._nav) if forward else jump_back(self._nav)
        if moved == self._nav:
            self.notify('No further jumps that way', severity='information')
            return
        self._nav = moved
        self.switch_screen(self.build_screen(current(moved)))

    def nav_sibling(self, targets: Sequence[NavTarget], *, offset: int) -> None:
        """Replace the current view with its neighbour in the given list."""
        if not targets:
            self.notify('Nothing to cycle through here', severity='information')
            return
        target = sibling(self._nav, targets, offset=offset)
        if target is None:
            self.notify('Nothing to cycle through here', severity='information')
            return
        self._nav = replace_top(self._nav, target)
        self.switch_screen(self.build_screen(target))

    def _sync_breadcrumb(self) -> None:
        screen = self.screen
        if isinstance(screen, ShellScreen):
            screen.update_breadcrumb()

    def command_registry(self, screen: ShellScreen) -> dict[str, ShellCommand]:
        """The global commands plus the ones this view adds."""
        return {**self._commands, **screen.commands()}

    def argument_values(self, args: Sequence[str]) -> list[str]:
        """Expand an argument placeholder into concrete completions."""
        match args:
            case ('<dashboard>',):
                return self.session.dashboard_names
            case ('<group>',):
                return [f'@{name}' for name in sorted(self.config_data.presets)] + self.session.group_names
            case _:
                return list(args)

    def run_command(self, screen: ShellScreen, text: str) -> None:
        """Dispatch a ``:`` command to the view first, then the global set."""
        name, *rest = text.split()
        argument = ' '.join(rest)
        if screen.run_view_command(name, argument):
            return
        if self._run_navigation_command(name, argument):
            return
        match name:
            case 'range':
                self._command_range(argument)
            case 'filter':
                self._command_filter(argument)
            case 'help':
                self._command_help(screen)
            case 'quit':
                self.exit()
            case _:
                self.notify(f'Unknown command: {name}', severity='warning')

    def _run_navigation_command(self, name: str, argument: str) -> bool:
        match name:
            case 'groups':
                self.goto(NavTarget(kind=ViewKind.GROUPS, label='groups'))
            case 'dashboards':
                self.goto(NavTarget(kind=ViewKind.DASHBOARDS, label='dashboards'))
            case 'dash':
                self._command_dash(argument)
            case 'logs':
                self._command_logs(argument, live=False)
            case 'tail':
                self._command_logs(argument, live=True)
            case _:
                return False
        return True

    def _command_logs(self, argument: str, *, live: bool) -> None:
        if argument:
            try:
                groups = expand_presets(argument.split(), self.config_data.presets)
            except ValueError as err:
                self.notify(str(err), severity='warning')
                return
        else:
            groups = list(self.session.selected_groups)
        if not groups:
            self.notify('Select a group first, or name one: :logs <group>', severity='warning')
            return
        self.open_logs(groups, live=live)

    def open_logs(self, groups: Sequence[str], *, live: bool = False) -> None:
        """Open the log view over the given groups, capped at the live-tail limit."""
        capped = list(groups)[:MAX_SELECTED_GROUPS]
        if len(groups) > MAX_SELECTED_GROUPS:
            self.notify(f'Showing the first {MAX_SELECTED_GROUPS} of {len(groups)} groups', severity='warning')
        self.session.selected_groups = capped
        self._record_recents(capped)
        label = 'tail' if live else 'logs'
        suffix = capped[0] if len(capped) == 1 else f'{len(capped)} groups'
        self.goto(NavTarget(kind=ViewKind.LOGS, label=f'{label} {suffix}', payload=tuple(capped)))

    def recent_groups(self) -> tuple[str, ...]:
        """The groups most recently opened under the session's profile, newest first."""
        return profile_recents(self.recents, self.session.profile)

    def _record_recents(self, groups: Sequence[str]) -> None:
        self.recents = record_selection(self.recents, groups, profile=self.session.profile)
        try:
            save_recents(self.recents, self._recents_path)
        except OSError as err:
            self.notify(f'Could not save recent groups: {err}', severity='warning')

    def _command_dash(self, argument: str) -> None:
        if not argument:
            self.notify('Usage: :dash <dashboard>', severity='warning')
            return
        self.goto(NavTarget(kind=ViewKind.DASHBOARD, label=argument, payload=(argument,)))

    def _command_range(self, argument: str) -> None:
        delta = parse_duration(argument)
        if delta is None:
            self.notify(f'Usage: :range <{"|".join(_RANGE_CHOICES)}>', severity='warning')
            return
        end = datetime.now(tz=UTC)
        self.set_window(end - delta, end)
        self.notify(f'Window -> last {argument}', severity='information')

    def set_window(self, start: datetime, end: datetime) -> None:
        """Change the shared window and tell the current view to re-read it."""
        self.session.start = start
        self.session.end = end
        self._refresh_current()

    def _command_filter(self, argument: str) -> None:
        self.session.filter_pattern = argument or None
        self.notify(f'Filter -> {argument}' if argument else 'Filter cleared', severity='information')
        self._refresh_current()

    def _refresh_current(self) -> None:
        screen = self.screen
        if isinstance(screen, ShellScreen):
            screen.update_breadcrumb()
            screen.refresh_view()

    def _command_help(self, screen: ShellScreen) -> None:
        registry = self.command_registry(screen)
        lines = [f':{name} — {command.summary}' for name, command in sorted(registry.items())]
        self.notify('\n'.join(lines), title='Commands', timeout=15)

    def show_which_key(self, screen: ShellScreen) -> None:
        """Push the reference of every binding and command available here."""
        keys = [
            (binding.key_display or binding.key, binding.description)
            for source in (screen, ShellScreen)
            for binding in getattr(source, 'BINDINGS', [])
            if isinstance(binding, Binding) and binding.description
        ]
        commands = [(name, command.summary) for name, command in sorted(self.command_registry(screen).items())]
        self.push_screen(WhichKeyScreen(keys, commands))

    def dive(self, widget: Widget, candidates: Sequence[DiveCandidate]) -> None:
        """Open the dive confirmation for a dashboard widget's candidate groups."""
        self.push_screen(DiveConfirmScreen(widget, list(candidates)))
