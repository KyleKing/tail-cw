"""Pytest configuration."""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _hermetic_aws_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Cut every test off from the machine's AWS configuration.

    Code paths that build a real client (the ``export`` CLI commands) resolve a
    region and credentials before any injected fake takes over, so without this a
    suite that passes on a developer machine with ``~/.aws/config`` fails on a bare
    CI runner with ``NoRegionError``. The credentials are deliberately fake so a
    test can never sign a request with real ones.
    """
    for name in ('AWS_PROFILE', 'AWS_REGION', 'AWS_SESSION_TOKEN'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('AWS_CONFIG_FILE', str(tmp_path / 'absent-aws-config'))
    monkeypatch.setenv('AWS_SHARED_CREDENTIALS_FILE', str(tmp_path / 'absent-aws-credentials'))
    monkeypatch.setenv('AWS_ACCESS_KEY_ID', 'testing')
    monkeypatch.setenv('AWS_SECRET_ACCESS_KEY', 'testing')
    monkeypatch.setenv('AWS_DEFAULT_REGION', 'us-east-1')


@pytest.fixture(autouse=True)
def recents_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the recents file at tmp_path so no test writes to the user data directory.

    Returns:
        Path: Where recents are read from and written to for this test

    """
    path = tmp_path / 'recents.json'
    monkeypatch.setattr('tail_cw.recents.recents_path', lambda: path)
    return path


@pytest.fixture
def fix_test_cache(tmp_path: Path) -> Path:
    """Return a cache directory private to this test.

    Per-test rather than one shared directory, so tests cannot delete each other's
    Parquet files and the suite is safe to run in parallel.

    Returns:
        Path: Path to an empty cache directory for this test

    """
    cache_dir = tmp_path / 'cache_root'
    cache_dir.mkdir()
    return cache_dir
