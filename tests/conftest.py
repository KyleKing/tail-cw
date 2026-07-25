"""Pytest configuration."""

from pathlib import Path

import pytest


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
