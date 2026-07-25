"""Tests for the pure navigation stack, jumplist, and breadcrumb."""

from __future__ import annotations

from pathlib import Path

from tail_cw.tui import navigation
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
    sibling,
)

_GROUPS = NavTarget(kind=ViewKind.GROUPS, label='groups')
_LOGS = NavTarget(kind=ViewKind.LOGS, label='/aws/lambda/api', payload=('/aws/lambda/api',))
_DASHBOARDS = NavTarget(kind=ViewKind.DASHBOARDS, label='dashboards')
_DASHBOARD = NavTarget(kind=ViewKind.DASHBOARD, label='prod-overview', payload=('prod-overview',))


def test_module_does_not_import_textual() -> None:
    source = Path(navigation.__file__).read_text(encoding='utf-8')
    assert 'textual' not in source
    assert not any(name.startswith('textual') for name in vars(navigation))


def test_initial_roots_stack_and_jumplist() -> None:
    state = initial(_GROUPS)
    assert state == NavState(stack=(_GROUPS,), jumps=((_GROUPS,),), jump_index=0)


def test_push_appends_to_stack_and_jumplist() -> None:
    state = push(initial(_GROUPS), _LOGS)
    assert state.stack == (_GROUPS, _LOGS)
    assert state.jumps == ((_GROUPS,), (_GROUPS, _LOGS))
    assert state.jump_index == 1


def test_push_does_not_mutate_the_input_state() -> None:
    state = initial(_GROUPS)
    push(state, _LOGS)
    assert state.stack == (_GROUPS,)
    assert state.jumps == ((_GROUPS,),)


def test_push_truncates_forward_jump_history() -> None:
    state = push(push(initial(_GROUPS), _DASHBOARDS), _DASHBOARD)
    state = jump_back(jump_back(state))
    assert state.jump_index == 0

    state = push(state, _LOGS)
    assert state.jumps == ((_GROUPS,), (_GROUPS, _LOGS))
    assert state.jump_index == 1


def test_pop_removes_the_top_of_the_stack() -> None:
    state = pop(push(initial(_GROUPS), _LOGS))
    assert state.stack == (_GROUPS,)


def test_pop_preserves_the_jumplist() -> None:
    state = pop(push(initial(_GROUPS), _LOGS))
    assert state.jumps == ((_GROUPS,), (_GROUPS, _LOGS))
    assert state.jump_index == 1
    assert jump_forward(jump_back(state)).jump_index == 1


def test_pop_at_root_is_a_no_op() -> None:
    state = initial(_GROUPS)
    assert pop(state) == state


def test_jump_back_stops_at_the_oldest_entry() -> None:
    state = push(initial(_GROUPS), _LOGS)
    assert jump_back(state).jump_index == 0
    assert jump_back(jump_back(jump_back(state))).jump_index == 0


def test_jump_forward_stops_at_the_newest_entry() -> None:
    state = push(push(initial(_GROUPS), _DASHBOARDS), _DASHBOARD)
    assert jump_forward(state).jump_index == 2
    assert jump_forward(jump_back(state)).jump_index == 2


def test_jump_round_trip_returns_to_the_same_target() -> None:
    state = push(push(initial(_GROUPS), _DASHBOARD), _LOGS)
    back = jump_back(state)
    assert current(back) == _DASHBOARD
    assert back.stack == (_GROUPS, _DASHBOARD)
    forward = jump_forward(back)
    assert current(forward) == _LOGS
    assert forward.stack == (_GROUPS, _DASHBOARD, _LOGS)


def test_jumps_on_an_empty_jumplist_are_no_ops() -> None:
    state = NavState(stack=(_GROUPS,), jumps=(), jump_index=0)
    assert jump_back(state) == state
    assert jump_forward(state) == state


def test_sibling_moves_forward_and_wraps() -> None:
    targets = [_DASHBOARD, _DASHBOARDS, _GROUPS]
    state = push(initial(_GROUPS), _DASHBOARD)
    assert sibling(state, targets, offset=1) == _DASHBOARDS
    assert sibling(state, targets, offset=-1) == _GROUPS


def test_sibling_wraps_backward_from_the_first_entry() -> None:
    targets = (_DASHBOARD, _DASHBOARDS)
    state = push(initial(_GROUPS), _DASHBOARDS)
    assert sibling(state, targets, offset=1) == _DASHBOARD


def test_sibling_returns_none_when_current_target_is_absent() -> None:
    state = push(initial(_GROUPS), _LOGS)
    assert sibling(state, [_DASHBOARD, _DASHBOARDS], offset=1) is None


def test_sibling_returns_none_for_an_empty_target_list() -> None:
    assert sibling(initial(_GROUPS), [], offset=1) is None


def test_breadcrumb_joins_stack_labels() -> None:
    state = push(push(initial(_DASHBOARDS), _DASHBOARD), _LOGS)
    assert breadcrumb(state) == 'dashboards › prod-overview › /aws/lambda/api'  # noqa: RUF001


def test_breadcrumb_of_the_root_is_one_label() -> None:
    assert breadcrumb(initial(_GROUPS)) == 'groups'


def test_view_kind_values_are_command_names() -> None:
    assert [str(kind) for kind in ViewKind] == ['groups', 'logs', 'dashboards', 'dashboard']
