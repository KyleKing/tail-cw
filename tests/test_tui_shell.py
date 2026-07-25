"""Tests for the shell app: navigation, the command line, and shared session state.

The views are stubbed so these exercise the shell in isolation; the real
screens are covered by their own modules.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from textual.app import App, ComposeResult
from textual.pilot import Pilot
from textual.widgets import Label

from tail_cw.cli import Session
from tail_cw.config import TailCWConfig
from tail_cw.recents import Recents, load_recents, save_recents
from tail_cw.tui.command_bar import CommandLine
from tail_cw.tui.navigation import NavTarget, ViewKind
from tail_cw.tui.shell import (
    MAX_SELECTED_GROUPS,
    ShellCommand,
    ShellScreen,
    ShellServices,
    TailCWApp,
    parse_duration,
)

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)


class StubScreen(ShellScreen):
    """A minimal view that records the refreshes it was asked to do."""

    def __init__(self, target: NavTarget) -> None:
        """Record the target this stub stands in for."""
        super().__init__()
        self.target = target
        self.refresh_count = 0
        self.focus_restored = 0
        self.handled: list[tuple[str, str]] = []

    def compose_content(self) -> ComposeResult:
        """Yield a body naming the target.

        Yields:
            One label.
        """
        yield Label(self.target.label, id='stub_body')

    def refresh_view(self) -> None:
        """Count the refreshes the shell asked for."""
        self.refresh_count += 1

    def restore_focus(self) -> None:
        """Count the focus handbacks."""
        self.focus_restored += 1


class StubDashboardScreen(StubScreen):
    """A view that owns a command and cycles the session's dashboards."""

    def commands(self) -> dict[str, ShellCommand]:  # ruff: ignore[no-self-use]
        """Add one view-specific command."""
        return {'panels': ShellCommand('Filter panels')}

    def run_view_command(self, name: str, argument: str) -> bool:
        """Claim `panels` and record it, refusing everything else."""
        if name == 'panels':
            self.handled.append((name, argument))
            return True
        return False

    def nav_siblings(self) -> list[NavTarget]:
        """Cycle the dashboards the session knows about."""
        return [
            NavTarget(kind=ViewKind.DASHBOARD, label=name, payload=(name,))
            for name in self.shell.session.dashboard_names
        ]


def _build_screen(target: NavTarget) -> ShellScreen:
    if target.kind is ViewKind.DASHBOARD:
        return StubDashboardScreen(target)
    return StubScreen(target)


def _session(**overrides: object) -> Session:
    defaults: dict[str, object] = {'start': NOW - timedelta(hours=1), 'end': NOW}
    return Session(**{**defaults, **overrides})  # type: ignore[arg-type]


def _app(
    *,
    session: Session | None = None,
    services: ShellServices | None = None,
    target: NavTarget | None = None,
    config: TailCWConfig | None = None,
    recents_path: Path | None = None,
) -> TailCWApp:
    return TailCWApp(
        config if config is not None else TailCWConfig(),
        session if session is not None else _session(),
        build_screen=_build_screen,
        services=services,
        target=target,
        recents_path=recents_path,
    )


@asynccontextmanager
async def _running(app: TailCWApp) -> AsyncIterator[Pilot[None]]:
    """Run the app and let it settle before navigating.

    Yields:
        The pilot driving the running app.
    """
    async with app.run_test() as pilot:
        await pilot.pause()
        yield pilot


async def test_opens_on_the_groups_view_by_default():
    """With no target the shell lands on the group browser."""
    app = _app()
    async with _running(app) as _pilot:
        assert isinstance(app.screen, StubScreen)
        assert app.screen.target.kind is ViewKind.GROUPS


async def test_opens_on_the_requested_target():
    """A seeded target chooses the opening view."""
    target = NavTarget(kind=ViewKind.DASHBOARD, label='prod', payload=('prod',))
    app = _app(target=target)
    async with _running(app) as _pilot:
        assert app.screen.target.kind is ViewKind.DASHBOARD  # type: ignore[attr-defined]


async def test_breadcrumb_shows_the_path_and_window():
    """The breadcrumb names every level and the shared window."""
    app = _app()
    async with _running(app) as pilot:
        app.goto(NavTarget(kind=ViewKind.DASHBOARD, label='prod', payload=('prod',)))
        await pilot.pause()
        text = str(app.screen.query_one('#breadcrumb', Label).render())
        assert 'groups' in text
        assert 'prod' in text
        assert '2026-07-24' in text


async def test_goto_then_pop_returns_to_the_previous_view():
    """Esc pops one level off the stack."""
    app = _app()
    async with _running(app) as _pilot:
        app.goto(NavTarget(kind=ViewKind.DASHBOARD, label='prod', payload=('prod',)))
        pushed = app.nav.stack
        app.nav_pop()
        popped = app.nav.stack
        assert len(pushed) == 2
        assert len(popped) == 1
        assert popped[-1].kind is ViewKind.GROUPS


async def test_pop_at_the_root_is_a_no_op():
    """The stack never empties."""
    app = _app()
    async with _running(app) as _pilot:
        app.nav_pop()
        assert len(app.nav.stack) == 1


async def test_jumplist_round_trips_back_and_forward():
    """Ctrl+O then Ctrl+I returns to where the jump started."""
    app = _app()
    async with _running(app) as _pilot:
        target = NavTarget(kind=ViewKind.DASHBOARD, label='prod', payload=('prod',))
        app.goto(target)
        assert app.nav.stack[-1] == target
        app.nav_jump(forward=False)
        assert app.nav.stack[-1].kind is ViewKind.GROUPS
        app.nav_jump(forward=True)
        assert app.nav.stack[-1] == target


async def test_jump_past_the_edge_notifies_and_holds():
    """Walking off either end of the jumplist changes nothing."""
    app = _app()
    async with _running(app) as _pilot:
        before = app.nav
        app.nav_jump(forward=True)
        assert app.nav == before
        app.nav_jump(forward=False)
        assert app.nav == before


async def test_sibling_cycles_within_the_level():
    """`]` replaces the current view with its neighbour rather than stacking."""
    session = _session(dashboard_names=['a', 'b', 'c'])
    app = _app(session=session, target=NavTarget(kind=ViewKind.DASHBOARD, label='a', payload=('a',)))
    async with _running(app) as _pilot:
        depth = len(app.nav.stack)
        app.nav_sibling(app.screen.nav_siblings(), offset=1)  # type: ignore[attr-defined]
        assert app.nav.stack[-1].label == 'b'
        assert len(app.nav.stack) == depth


async def test_sibling_with_no_targets_notifies():
    """Cycling where there are no siblings leaves the stack alone."""
    app = _app()
    async with _running(app) as _pilot:
        before = app.nav
        app.nav_sibling([], offset=1)
        assert app.nav == before


async def test_range_command_moves_the_window_and_refreshes():
    """`:range` rewrites the shared window and tells the view to re-read it."""
    app = _app()
    async with _running(app) as pilot:
        screen = app.screen
        assert isinstance(screen, StubScreen)
        app.run_command(screen, 'range 6h')
        await pilot.pause()
        assert app.session.end - app.session.start == timedelta(hours=6)
        assert screen.refresh_count == 1


async def test_range_command_rejects_a_bad_duration():
    """A malformed range leaves the window untouched."""
    app = _app()
    async with _running(app) as _pilot:
        screen = app.screen
        assert isinstance(screen, StubScreen)
        before = (app.session.start, app.session.end)
        app.run_command(screen, 'range banana')
        assert (app.session.start, app.session.end) == before
        assert screen.refresh_count == 0


async def test_filter_command_sets_and_clears_the_shared_filter():
    """`:filter` with text sets it; bare `:filter` clears it."""
    app = _app()
    async with _running(app) as _pilot:
        screen = app.screen
        assert isinstance(screen, StubScreen)
        app.run_command(screen, 'filter ERROR')
        after_set = app.session.filter_pattern
        app.run_command(screen, 'filter')
        after_clear = app.session.filter_pattern
        assert after_set == 'ERROR'
        assert after_clear is None
        assert screen.refresh_count == 2


async def test_view_command_takes_precedence_over_the_global_set():
    """A view handles its own command and the shell does not see it."""
    app = _app(target=NavTarget(kind=ViewKind.DASHBOARD, label='prod', payload=('prod',)))
    async with _running(app) as _pilot:
        screen = app.screen
        assert isinstance(screen, StubDashboardScreen)
        app.run_command(screen, 'panels errors')
        assert screen.handled == [('panels', 'errors')]


async def test_unknown_command_is_reported_not_raised():
    """An unrecognised command notifies instead of failing."""
    app = _app()
    async with _running(app) as _pilot:
        screen = app.screen
        assert isinstance(screen, ShellScreen)
        app.run_command(screen, 'nonsense arg')
        assert len(app.nav.stack) == 1


@pytest.mark.parametrize(
    ('command', 'expected'),
    [
        ('groups', ViewKind.GROUPS),
        ('dashboards', ViewKind.DASHBOARDS),
        ('dash prod', ViewKind.DASHBOARD),
    ],
)
async def test_view_switching_commands(command: str, expected: ViewKind):
    """The global commands navigate to their view."""
    app = _app()
    async with _running(app) as _pilot:
        screen = app.screen
        assert isinstance(screen, ShellScreen)
        app.run_command(screen, command)
        assert app.nav.stack[-1].kind is expected


async def test_dash_command_without_a_name_is_refused():
    """`:dash` needs a dashboard name."""
    app = _app()
    async with _running(app) as _pilot:
        screen = app.screen
        assert isinstance(screen, ShellScreen)
        app.run_command(screen, 'dash')
        assert len(app.nav.stack) == 1


async def test_logs_command_uses_the_named_groups():
    """`:logs <group>` opens that group rather than the selection."""
    app = _app(session=_session(selected_groups=['/aws/lambda/other']))
    async with _running(app) as _pilot:
        screen = app.screen
        assert isinstance(screen, ShellScreen)
        app.run_command(screen, 'logs /aws/lambda/api')
        assert app.nav.stack[-1].payload == ('/aws/lambda/api',)
        assert app.session.selected_groups == ['/aws/lambda/api']


async def test_logs_command_falls_back_to_the_selection():
    """Bare `:logs` uses whatever the browser selected."""
    app = _app(session=_session(selected_groups=['/aws/lambda/api']))
    async with _running(app) as _pilot:
        screen = app.screen
        assert isinstance(screen, ShellScreen)
        app.run_command(screen, 'logs')
        assert app.nav.stack[-1].payload == ('/aws/lambda/api',)


async def test_logs_command_with_nothing_selected_is_refused():
    """With no selection and no argument there is nothing to open."""
    app = _app()
    async with _running(app) as _pilot:
        screen = app.screen
        assert isinstance(screen, ShellScreen)
        app.run_command(screen, 'logs')
        assert len(app.nav.stack) == 1


async def test_tail_command_labels_the_target_as_live():
    """`:tail` seeds the log view in live mode."""
    app = _app()
    async with _running(app) as _pilot:
        screen = app.screen
        assert isinstance(screen, ShellScreen)
        app.run_command(screen, 'tail /aws/lambda/api')
        assert app.nav.stack[-1].label.startswith('tail')


async def test_open_logs_caps_the_group_count():
    """The selection is trimmed to the Live Tail limit."""
    app = _app()
    groups = [f'/aws/lambda/fn-{index}' for index in range(MAX_SELECTED_GROUPS + 4)]
    async with _running(app) as _pilot:
        app.open_logs(groups)
        assert len(app.nav.stack[-1].payload) == MAX_SELECTED_GROUPS
        assert app.session.selected_groups == groups[:MAX_SELECTED_GROUPS]


async def test_open_logs_labels_a_multi_group_view_by_count():
    """Several groups read as a count rather than a truncated name."""
    app = _app()
    async with _running(app) as _pilot:
        app.open_logs(['/a', '/b'])
        assert '2 groups' in app.nav.stack[-1].label


async def test_open_logs_records_the_selection_in_recents(tmp_path: Path):
    """Opening a view is the moment a selection is resolved, so it is recorded."""
    path = tmp_path / 'recents.json'
    app = _app(recents_path=path)
    async with _running(app) as _pilot:
        app.open_logs(['/a', '/b'])
        app.open_logs(['/c'])
        assert app.recent_groups() == ('/c', '/a', '/b')
        assert load_recents(path).by_profile == {'': ('/c', '/a', '/b')}


async def test_recents_are_scoped_to_the_profile(tmp_path: Path):
    """A group recorded under one profile stays out of another's history."""
    path = tmp_path / 'recents.json'
    save_recents(Recents(by_profile={'dev': ('/dev-only',)}), path)
    app = _app(session=_session(profile='prod'), recents_path=path)
    async with _running(app) as _pilot:
        app.open_logs(['/prod-only'])
        assert app.recent_groups() == ('/prod-only',)
        assert load_recents(path).by_profile['dev'] == ('/dev-only',)


async def test_a_failed_recents_write_notifies_instead_of_crashing(monkeypatch: pytest.MonkeyPatch):
    """The history is a convenience; losing it must not break navigation."""

    def _explode(_recents: Recents, _path: Path | None = None) -> None:
        msg = 'disk full'
        raise OSError(msg)

    monkeypatch.setattr('tail_cw.tui.shell.save_recents', _explode)
    app = _app()
    async with _running(app) as _pilot:
        app.open_logs(['/a'])
        assert app.recent_groups() == ('/a',)
        assert app.nav.stack[-1].payload == ('/a',)


async def test_logs_command_expands_a_preset():
    """`:logs @api` opens the preset's groups."""
    app = _app(config=TailCWConfig(presets={'api': ['/aws/lambda/api-a', '/ecs/api-b']}))
    async with _running(app) as _pilot:
        screen = app.screen
        assert isinstance(screen, ShellScreen)
        app.run_command(screen, 'logs @api')
        assert app.nav.stack[-1].payload == ('/aws/lambda/api-a', '/ecs/api-b')


async def test_tail_command_on_an_unknown_preset_does_not_navigate():
    """An unknown `@name` warns rather than opening an empty view."""
    app = _app(config=TailCWConfig(presets={'api': ['/a']}))
    async with _running(app) as _pilot:
        screen = app.screen
        assert isinstance(screen, ShellScreen)
        depth = len(app.nav.stack)
        app.run_command(screen, 'tail @web')
        assert len(app.nav.stack) == depth


async def test_completion_offers_presets_before_group_names():
    """Tab suggests `@name` wherever a group is accepted."""
    session = _session(group_names=['/aws/lambda/api'])
    app = _app(session=session, config=TailCWConfig(presets={'api': ['/aws/lambda/api']}))
    async with _running(app) as _pilot:
        screen = app.screen
        assert isinstance(screen, ShellScreen)
        assert screen.complete_command('logs @') == ['logs @api']


async def test_command_registry_merges_view_commands():
    """A view's commands join the global set for completion and help."""
    app = _app(target=NavTarget(kind=ViewKind.DASHBOARD, label='prod', payload=('prod',)))
    async with _running(app) as _pilot:
        screen = app.screen
        assert isinstance(screen, ShellScreen)
        registry = app.command_registry(screen)
        assert 'panels' in registry
        assert 'range' in registry


async def test_completion_offers_command_names_then_arguments():
    """Tab completes a command name, then that command's argument values."""
    session = _session(dashboard_names=['prod-overview', 'prod-billing'], group_names=['/aws/lambda/api'])
    app = _app(session=session)
    async with _running(app) as _pilot:
        screen = app.screen
        assert isinstance(screen, ShellScreen)
        assert screen.complete_command('da') == ['dash', 'dashboards']
        assert screen.complete_command('dash prod-o') == ['dash prod-overview']
        assert screen.complete_command('logs /aws') == ['logs /aws/lambda/api']
        assert screen.complete_command('range 6') == ['range 6h']


async def test_completion_of_an_unknown_command_offers_nothing():
    """An unrecognised name has no arguments to offer."""
    app = _app()
    async with _running(app) as _pilot:
        screen = app.screen
        assert isinstance(screen, ShellScreen)
        assert screen.complete_command('nonsense ') == []


async def test_submitting_the_command_line_runs_it_and_restores_focus():
    """Enter in the command line dispatches and hands focus back to the view."""
    app = _app()
    async with _running(app) as pilot:
        screen = app.screen
        assert isinstance(screen, StubScreen)
        line = screen.query_one(CommandLine)
        line.open()
        line.value = 'filter ERROR'
        await pilot.press('enter')
        await pilot.pause()
        assert app.session.filter_pattern == 'ERROR'
        assert screen.focus_restored == 1
        assert line.display is False


async def test_which_key_lists_bindings_and_commands():
    """`?` pushes the reference over the current view."""
    app = _app()
    async with _running(app) as pilot:
        screen = app.screen
        assert isinstance(screen, ShellScreen)
        app.show_which_key(screen)
        await pilot.pause()
        assert app.screen is not screen


async def test_help_command_does_not_navigate():
    """`:help` notifies without changing the view."""
    app = _app()
    async with _running(app) as _pilot:
        screen = app.screen
        assert isinstance(screen, ShellScreen)
        app.run_command(screen, 'help')
        assert app.screen is screen


async def test_quit_command_exits():
    """`:quit` leaves the app."""
    app = _app()
    async with _running(app) as pilot:
        screen = app.screen
        assert isinstance(screen, ShellScreen)
        app.run_command(screen, 'quit')
        await pilot.pause()
    assert app.return_value is None


async def test_services_default_to_empty():
    """The app runs with no services wired, for offline use and tests."""
    app = _app()
    async with _running(app) as _pilot:
        assert app.services.list_groups is None
        assert app.services.resolve_logs is None


class PlainApp(App[None]):
    """A host that is not a TailCWApp, to prove the shell property complains."""

    def get_default_screen(self) -> ShellScreen:  # ruff: ignore[no-self-use]
        """Mount a ShellScreen under the wrong kind of app."""
        return StubScreen(NavTarget(kind=ViewKind.GROUPS, label='groups'))


async def test_shell_property_rejects_a_foreign_host():
    """A ShellScreen mounted outside a TailCWApp says so rather than failing late."""
    app = PlainApp()
    with pytest.raises(TypeError, match='TailCWApp'):
        async with app.run_test():
            pass


@pytest.mark.parametrize(
    ('text', 'expected'),
    [
        ('15m', timedelta(minutes=15)),
        ('2h', timedelta(hours=2)),
        ('3d', timedelta(days=3)),
        (' 6h ', timedelta(hours=6)),
        ('', None),
        ('h', None),
        ('6', None),
        ('6y', None),
        ('banana', None),
        ('-1h', None),
    ],
)
def test_parse_duration(text: str, expected: timedelta | None):
    """Durations parse, and anything else is refused rather than guessed."""
    assert parse_duration(text) == expected


def test_session_window_label_is_compact():
    """The window renders as a single status-line fragment."""
    assert _session().window_label() == '2026-07-24 11:00->12:00 UTC'
