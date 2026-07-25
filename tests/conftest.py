"""Pytest configuration."""

from pathlib import Path

import pytest

from .configuration import TEST_TMP_CACHE, clear_test_cache


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
def fix_test_cache() -> Path:
    """Fixture to clear and return the test cache directory for use.

    Returns:
        Path: Path to the test cache directory

    """
    clear_test_cache()
    return TEST_TMP_CACHE
