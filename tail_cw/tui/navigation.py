"""Navigation stack, jumplist, and breadcrumb for the single-app shell.

Pure state transitions with no Textual import, so every motion the shell binds
(``Esc``, ``Ctrl+O``, ``Ctrl+I``, ``[``, ``]``) is unit-testable without a
terminal. Jumplist semantics copy vim: a push truncates forward history, and
walking back or forward stops at the ends rather than wrapping.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from enum import StrEnum

from tail_cw.text import ELLIPSIS, shorten

BREADCRUMB_SEPARATOR = ' › '  # ruff: ignore[ambiguous-unicode-character-string]
HEADER_SEPARATOR = '  ·  '
"""What separates the tool name, the navigation path, and the window in the header."""
"""Separator joining stack labels in the header breadcrumb."""


class ViewKind(StrEnum):
    """The kind of view a navigation target opens."""

    GROUPS = 'groups'
    LOGS = 'logs'
    DASHBOARDS = 'dashboards'
    DASHBOARD = 'dashboard'
    REPORT = 'report'
    XRAY = 'xray'


@dataclass(frozen=True)
class NavTarget:
    """One navigable destination.

    Attributes:
        kind: Which view renders the target.
        label: Breadcrumb text for the target.
        payload: View-specific arguments, such as selected log groups or a
            dashboard name.
        argument: One view-specific value that is not part of the payload list,
            such as the trace id a log view opens on.
    """

    kind: ViewKind
    label: str
    payload: tuple[str, ...] = ()
    argument: str = ''


@dataclass(frozen=True)
class NavState:
    """The view stack plus a vim-style jumplist over visited stacks.

    A jumplist entry is a whole stack rather than a single target, so jumping
    restores the path that reached a view and the breadcrumb stays truthful.
    Recording only the target would leave the breadcrumb showing the depth you
    jumped from.

    Attributes:
        stack: Targets from the root view to the current one, never empty.
        jumps: The stack as it stood at each push, oldest first.
        jump_index: Position in ``jumps`` that the jumplist cursor sits on.
    """

    stack: tuple[NavTarget, ...]
    jumps: tuple[tuple[NavTarget, ...], ...]
    jump_index: int


def current(state: NavState) -> NavTarget:
    """The target the user is looking at."""
    return state.stack[-1]


def initial(target: NavTarget) -> NavState:
    """Build the opening state rooted at one target."""
    stack = (target,)
    return NavState(stack=stack, jumps=(stack,), jump_index=0)


def push(state: NavState, target: NavTarget) -> NavState:
    """Open a target above the current one, discarding forward jump history."""
    stack = (*state.stack, target)
    jumps = (*state.jumps[: state.jump_index + 1], stack)
    return NavState(stack=stack, jumps=jumps, jump_index=len(jumps) - 1)


def pop(state: NavState) -> NavState:
    """Close the top view, leaving the jumplist intact; a no-op at the root."""
    if len(state.stack) <= 1:
        return state
    return replace(state, stack=state.stack[:-1])


def replace_top(state: NavState, target: NavTarget) -> NavState:
    """Swap the current view for a peer, keeping the depth and recording a jump.

    This is what ``[`` and ``]`` do. Popping and pushing would be wrong at the
    root, where a pop is a no-op and the stack would grow instead.
    """
    stack = (*state.stack[:-1], target)
    jumps = (*state.jumps[: state.jump_index + 1], stack)
    return NavState(stack=stack, jumps=jumps, jump_index=len(jumps) - 1)


def _jump_to(state: NavState, index: int) -> NavState:
    return NavState(stack=state.jumps[index], jumps=state.jumps, jump_index=index)


def jump_back(state: NavState) -> NavState:
    """Restore the stack one jumplist entry older, stopping at the oldest."""
    if not state.jumps:
        return state
    return _jump_to(state, max(state.jump_index - 1, 0))


def jump_forward(state: NavState) -> NavState:
    """Restore the stack one jumplist entry newer, stopping at the newest."""
    if not state.jumps:
        return state
    return _jump_to(state, min(state.jump_index + 1, len(state.jumps) - 1))


def sibling(state: NavState, targets: Sequence[NavTarget], *, offset: int) -> NavTarget | None:
    """Return the target ``offset`` places from the current one, wrapping around.

    Returns None when ``targets`` is empty or does not contain the current top of
    the stack, which is how the shell decides ``[`` and ``]`` do nothing.
    """
    if not targets or not state.stack:
        return None
    current = state.stack[-1]
    if current not in targets:
        return None
    return targets[(targets.index(current) + offset) % len(targets)]


def breadcrumb(state: NavState) -> str:
    """Render the stack labels as the header breadcrumb."""
    return BREADCRUMB_SEPARATOR.join(target.label for target in state.stack)


def fit_breadcrumb(app_name: str, path: Sequence[str], window: str, width: int) -> str:
    """Fit the header breadcrumb into ``width``, dropping whole parts rather than clipping.

    Dropped in order: the window, the tool name, then the outermost path entries,
    which are replaced by an ellipsis. A clipped breadcrumb ended on a separator
    with nothing after it, saying a part existed without saying which.

    Args:
        app_name: The tool's own name, the first thing worth losing after the window.
        path: Navigation labels, outermost first; the last is the current view.
        window: The shared time window.
        width: Cells the breadcrumb has.
    """
    # Trails from the full path down to the current view alone, each shortened one
    # entry from the outside and marked, so no trail is only an ellipsis.
    trails = [list(path), *([ELLIPSIS, *path[index:]] for index in range(1, len(path)))]
    for trail in trails:
        joined = BREADCRUMB_SEPARATOR.join(trail)
        for parts in ([app_name, joined, window], [app_name, joined], [joined]):
            candidate = HEADER_SEPARATOR.join(part for part in parts if part)
            if candidate and len(candidate) <= width:
                return candidate
    return shorten(path[-1] if path else app_name, max(1, width))
