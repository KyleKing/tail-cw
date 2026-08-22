"""Cover the shell completion source and the guard that keeps it out of startup."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from tail_cw.completion import ACTIVATION_VARIABLE, install, log_group_completer
from tail_cw.recents import Recents, load_recents, save_recents


@pytest.fixture
def _recents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the recents file at a per-test path, so parallel runs cannot collide."""
    target = tmp_path / 'recents.json'
    monkeypatch.setattr('tail_cw.completion.load_recents', lambda: _load(target))
    save_recents(
        Recents(
            by_profile={
                'read-prod': ('irm-ecs-api-prod', '/aws/lambda/api-handler', 'irm-ecs-worker'),
                # The empty key is what a profile-less invocation reads, per DEFAULT_PROFILE_KEY.
                '': ('demo/web-api',),
            },
        ),
        target,
    )


def _load(target: Path) -> Recents:
    return load_recents(target)


@pytest.mark.usefixtures('_recents')
def test_completion_offers_the_groups_recorded_for_that_profile() -> None:
    args = argparse.Namespace(profile='read-prod')

    assert log_group_completer('irm', args) == ['irm-ecs-api-prod', 'irm-ecs-worker']
    assert log_group_completer('/aws', args) == ['/aws/lambda/api-handler']
    assert len(log_group_completer('', args)) == 3


@pytest.mark.usefixtures('_recents')
def test_completion_matches_by_prefix_because_a_shell_replaces_the_word() -> None:
    """Substring resolution happens at run time instead, in the pattern resolver."""
    assert log_group_completer('handler', argparse.Namespace(profile='read-prod')) == []


@pytest.mark.usefixtures('_recents')
def test_the_profile_comes_from_the_flag_then_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('AWS_PROFILE', 'read-prod')

    assert log_group_completer('irm', argparse.Namespace(profile=None))
    assert log_group_completer('irm', argparse.Namespace(profile='other')) == []
    monkeypatch.delenv('AWS_PROFILE')
    assert log_group_completer('demo', argparse.Namespace(profile=None)) == ['demo/web-api']


def test_a_missing_recents_file_completes_nothing_rather_than_failing(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode() -> Recents:
        raise OSError

    monkeypatch.setattr('tail_cw.completion.load_recents', explode)

    assert log_group_completer('irm', argparse.Namespace(profile='read-prod')) == []


def test_install_does_nothing_unless_the_shell_is_completing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The entry point's whole job is to stay cheap, so argcomplete must not load."""
    monkeypatch.delenv(ACTIVATION_VARIABLE, raising=False)
    calls: list[object] = []
    monkeypatch.setattr('argcomplete.autocomplete', calls.append)

    install(argparse.ArgumentParser())
    assert calls == []

    monkeypatch.setenv(ACTIVATION_VARIABLE, '1')
    parser = argparse.ArgumentParser()
    install(parser)
    assert calls == [parser]
