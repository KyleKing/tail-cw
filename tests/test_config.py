"""Tests for the configuration module."""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import pytest

from tail_cw.config import (
    CacheConfig,
    ParquetConfig,
    TailCWConfig,
    TraceConfig,
    TUIConfig,
    create_default_config_file,
    get_default_cache_dir,
    get_default_config_path,
    load_config,
)
from tail_cw.query.trace import DEFAULT_TRACE_ID_FIELDS


@pytest.fixture
def xdg_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
    """Configure temporary XDG directories for tests.

    Returns:
        Tuple containing the config and cache directories.
    """
    config_dir = tmp_path / 'xdg_config'
    cache_dir = tmp_path / 'xdg_cache'
    monkeypatch.setenv('XDG_CONFIG_HOME', str(config_dir))
    monkeypatch.setenv('XDG_CACHE_HOME', str(cache_dir))
    return config_dir, cache_dir


def test_default_config_values():
    config = TailCWConfig()

    assert config.cache.cache_dir is None
    assert config.cache.size_limit_mb == 1000
    assert config.cache.default_ttl_seconds is None
    assert config.cache.eviction_policy == 'least-recently-stored'

    assert config.parquet.row_group_size == 100_000
    assert config.parquet.compression_level == 3
    assert config.parquet.infer_schema_length == 1000

    assert config.tui.chunk_threshold == 5000
    assert config.tui.chunk_size == 1000
    assert config.tui.initial_load_limit == 1000
    assert config.tui.search_limit == 10_000
    assert config.tui.trace_limit == 100

    assert config.trace.trace_id_fields == list(DEFAULT_TRACE_ID_FIELDS)


def test_get_default_config_path(xdg_paths: tuple[Path, Path]):
    _config_dir, _cache_dir = xdg_paths
    path = get_default_config_path()

    assert path.is_absolute()
    assert path.name == 'config.toml'
    assert path.parent.name == 'tail-cw'


def test_get_default_cache_dir(xdg_paths: tuple[Path, Path]):
    _config_dir, _cache_dir = xdg_paths
    path = get_default_cache_dir()

    assert path.is_absolute()
    assert path.name == 'tail-cw'


def test_load_config_nonexistent_file(xdg_paths: tuple[Path, Path], tmp_path: Path):
    missing_path = tmp_path / 'missing.toml'
    config = load_config(missing_path)

    assert isinstance(config, TailCWConfig)
    assert config.parquet.row_group_size == 100_000
    assert config.cache.cache_dir == get_default_cache_dir()


def test_load_config_valid_toml(xdg_paths: tuple[Path, Path], tmp_path: Path):
    config_path = tmp_path / 'custom.toml'
    custom_cache_dir = tmp_path / 'cache'
    config_path.write_text(
        '\n'.join(
            [
                '[cache]',
                f'cache_dir = "{custom_cache_dir.as_posix()}"',
                'size_limit_mb = 512',
                'default_ttl_seconds = 600',
                'eviction_policy = "least-frequently-stored"',
                '',
                '[parquet]',
                'row_group_size = 200000',
                'compression_level = 5',
                'infer_schema_length = 250',
                '',
                '[tui]',
                'chunk_threshold = 2000',
                'chunk_size = 250',
                'initial_load_limit = 750',
                'search_limit = 5000',
                'trace_limit = 50',
                '',
                '[trace]',
                'trace_id_fields = ["traceId", "context.trace_id"]',
            ],
        ),
        encoding='utf-8',
    )

    config = load_config(config_path)

    assert config.cache.cache_dir == custom_cache_dir.resolve()
    assert config.cache.size_limit_mb == 512
    assert config.cache.default_ttl_seconds == 600
    assert config.cache.eviction_policy == 'least-frequently-stored'

    assert config.parquet.row_group_size == 200_000
    assert config.parquet.compression_level == 5
    assert config.parquet.infer_schema_length == 250

    assert config.tui.chunk_threshold == 2000
    assert config.tui.chunk_size == 250
    assert config.tui.initial_load_limit == 750
    assert config.tui.search_limit == 5000
    assert config.tui.trace_limit == 50

    assert config.trace.trace_id_fields == ['traceId', 'context.trace_id']


def test_load_config_partial_toml(tmp_path: Path):
    config_path = tmp_path / 'partial.toml'
    config_path.write_text(
        '[cache]\nsize_limit_mb = 256',
        encoding='utf-8',
    )

    config = load_config(config_path)

    assert config.cache.size_limit_mb == 256
    assert config.parquet.row_group_size == 100_000
    assert config.tui.chunk_size == 1000


def test_load_config_invalid_toml(tmp_path: Path):
    config_path = tmp_path / 'invalid.toml'
    config_path.write_text('[cache\nsize_limit_mb = 256', encoding='utf-8')

    with pytest.raises(ValueError, match='Invalid configuration file'):
        load_config(config_path)


def test_create_default_config_file(tmp_path: Path):
    config_path = tmp_path / 'config' / 'config.toml'
    created = create_default_config_file(config_path)

    assert created == config_path
    assert config_path.exists()

    data = tomllib.loads(config_path.read_text(encoding='utf-8'))
    assert 'cache' in data
    assert 'parquet' in data
    assert 'tui' in data
    assert 'trace' in data


def test_create_default_config_file_atomic(tmp_path: Path):
    config_path = tmp_path / 'config' / 'config.toml'
    create_default_config_file(config_path)

    assert not any(config_path.parent.glob('*.tmp'))


def test_cache_config_dataclass(tmp_path: Path):
    cache_dir = tmp_path / 'cache'
    config = CacheConfig(cache_dir=cache_dir, size_limit_mb=256, default_ttl_seconds=120, eviction_policy='lru')

    assert config.cache_dir == cache_dir
    assert config.size_limit_mb == 256
    assert config.default_ttl_seconds == 120
    assert config.eviction_policy == 'lru'


def test_parquet_config_dataclass():
    config = ParquetConfig(row_group_size=64_000, compression_level=7, infer_schema_length=128)

    assert config.row_group_size == 64_000
    assert config.compression_level == 7
    assert config.infer_schema_length == 128


def test_tui_config_dataclass():
    config = TUIConfig(chunk_threshold=750, chunk_size=125, initial_load_limit=500, search_limit=2500, trace_limit=10)

    assert config.chunk_threshold == 750
    assert config.chunk_size == 125
    assert config.initial_load_limit == 500
    assert config.search_limit == 2500
    assert config.trace_limit == 10


def test_trace_config_dataclass():
    config = TraceConfig()
    other = TraceConfig(trace_id_fields=['traceId', 'context.trace_id'])

    assert config.trace_id_fields == list(DEFAULT_TRACE_ID_FIELDS)
    assert config.trace_id_fields is not DEFAULT_TRACE_ID_FIELDS
    assert other.trace_id_fields == ['traceId', 'context.trace_id']


def test_config_integration(tmp_path: Path):
    config_path = tmp_path / 'config.toml'
    cache_path = tmp_path / 'cache'
    config_path.write_text(
        '\n'.join(
            [
                '[cache]',
                f'cache_dir = "{cache_path.as_posix()}"',
                'size_limit_mb = 256',
                '',
                '[parquet]',
                'row_group_size = 120000',
                '',
                '[tui]',
                'chunk_threshold = 1500',
                '',
                '[trace]',
                'trace_id_fields = ["traceId"]',
            ],
        ),
        encoding='utf-8',
    )

    config = load_config(config_path)
    assert config.cache.size_limit_mb == 256
    assert config.parquet.row_group_size == 120_000
    assert config.tui.chunk_threshold == 1500
    assert config.trace.trace_id_fields == ['traceId']

    config_path.write_text(
        '[cache]\nsize_limit_mb = 512\n\n[parquet]\nrow_group_size = 220000\n\n[tui]\nchunk_threshold = 2500',
        encoding='utf-8',
    )

    updated = load_config(config_path)
    assert updated.cache.size_limit_mb == 512
    assert updated.parquet.row_group_size == 220_000
    assert updated.tui.chunk_threshold == 2500
    assert updated.trace.trace_id_fields == list(DEFAULT_TRACE_ID_FIELDS)


def test_config_with_custom_cache_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('USERPROFILE', str(tmp_path))
    config_path = tmp_path / 'config.toml'
    config_path.write_text(
        '[cache]\ncache_dir = "~/custom_cache"',
        encoding='utf-8',
    )

    config = load_config(config_path)
    assert config.cache.cache_dir == (tmp_path / 'custom_cache').resolve()


def test_config_xdg_compliance(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    config_dir = tmp_path / 'xdg_config'
    cache_dir = tmp_path / 'xdg_cache'
    monkeypatch.setenv('XDG_CONFIG_HOME', str(config_dir))
    monkeypatch.setenv('XDG_CACHE_HOME', str(cache_dir))

    config_path = get_default_config_path()
    cache_path = get_default_cache_dir()

    assert config_path.is_absolute()
    assert cache_path.is_absolute()

    if sys.platform.startswith('linux'):
        assert config_path.parent == config_dir / 'tail-cw'
        assert cache_path == cache_dir / 'tail-cw'
    else:
        assert config_path.parent.name == 'tail-cw'
        assert cache_path.name == 'tail-cw'

    assert config_path.parent.exists()
    assert cache_path.exists()
