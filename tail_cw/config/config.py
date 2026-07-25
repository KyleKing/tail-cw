"""Configuration loading and defaults for tail_cw.

This module exposes dataclasses describing the configurable aspects of the
application along with helpers for loading user-supplied TOML files from
XDG-compliant locations. Consumers can customize cache behaviour, Parquet
storage parameters, TUI pagination, and trace extraction without modifying
source code.
"""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Final

from platformdirs import user_cache_dir, user_config_dir

from tail_cw.query.trace import DEFAULT_TRACE_ID_FIELDS

DEFAULT_CONFIG_FILENAME: Final = 'config.toml'


def _expand_path(value: str | os.PathLike[str]) -> Path:
    """Expand user and environment variables in a path-like value.

    Returns:
        Absolute, expanded path for the provided value.
    """
    return Path(os.path.expandvars(Path(str(value)).expanduser())).resolve()


@dataclass(slots=True)
class CacheConfig:
    """Disk cache configuration.

    Attributes:
        cache_dir: Directory used for cached Parquet files. When ``None``,
            :func:`get_default_cache_dir` is used at runtime.
        size_limit_mb: Maximum cache size in megabytes before eviction happens.
        default_ttl_seconds: Default Time-To-Live applied to cache entries.
            ``None`` disables expiration.
        eviction_policy: Name of the eviction policy supplied to DiskCache.
    """

    cache_dir: Path | None = None
    size_limit_mb: int = 1000
    default_ttl_seconds: int | None = None
    eviction_policy: str = 'least-recently-stored'


@dataclass(slots=True)
class ParquetConfig:
    """Parquet conversion tuning parameters.

    Attributes:
        row_group_size: Number of rows per Parquet row group. Larger groups
            improve scan performance at the cost of memory.
        compression_level: ZSTD compression level (1-22). Higher levels trade
            speed for reduced file size.
        infer_schema_length: Number of rows sampled when inferring the schema
            from NDJSON payloads.
    """

    row_group_size: int = 100_000
    compression_level: int = 3
    infer_schema_length: int = 1000


@dataclass(slots=True)
class PreviewConfig:
    """Log group preview sampling parameters.

    Attributes:
        sample_limit: Maximum number of events read when sampling a group.
        window_seconds: Length of the recent window sampled for a preview.
        ttl_seconds: How long a cached preview stays fresh.
    """

    sample_limit: int = 500
    window_seconds: int = 900
    ttl_seconds: int = 300


@dataclass(slots=True)
class TUIConfig:
    """TUI behaviour and incremental loading parameters.

    Attributes:
        chunk_threshold: Minimum row count before incremental loading is used.
        chunk_size: Number of rows appended per incremental update.
        initial_load_limit: Initial number of rows to load when opening a
            Parquet dataset.
        live_buffer_limit: Maximum number of live tail events retained in the
            in-memory ring buffer; the oldest events are dropped once exceeded.
        search_limit: Maximum number of search results returned from queries.
        trace_limit: Maximum number of trace groups fetched when toggling the
            trace view.
    """

    chunk_threshold: int = 5000
    chunk_size: int = 1000
    initial_load_limit: int = 1000
    live_buffer_limit: int = 10_000
    search_limit: int = 10_000
    trace_limit: int = 100


@dataclass(slots=True)
class TraceConfig:
    """Trace extraction configuration.

    Attributes:
        trace_id_fields: Ordered list of field names used when searching for
            trace identifiers inside structured log payloads.
    """

    trace_id_fields: list[str] = field(default_factory=lambda: list(DEFAULT_TRACE_ID_FIELDS))


@dataclass(slots=True)
class TailCWConfig:
    """Container for all application configuration sections.

    The dataclass mirrors the structure of the TOML configuration file:

    .. code-block:: toml

        [cache]
        size_limit_mb = 1000
        eviction_policy = "least-recently-stored"

        [parquet]
        row_group_size = 100000
        compression_level = 3

        [preview]
        sample_limit = 500
        window_seconds = 900

        [tui]
        chunk_threshold = 5000
        chunk_size = 1000

        [trace]
        trace_id_fields = ["trace_id", "traceId"]

        [presets]
        api = ["/aws/lambda/api-a", "/ecs/api-b"]

    Attributes:
        cache: Cache persistence configuration.
        parquet: Parquet conversion configuration.
        preview: Log group preview sampling configuration.
        tui: TUI incremental loading configuration.
        trace: Trace extraction configuration.
        presets: Named log group sets, referenced as ``@name`` wherever a log
            group pattern is accepted.
    """

    cache: CacheConfig = field(default_factory=CacheConfig)
    parquet: ParquetConfig = field(default_factory=ParquetConfig)
    preview: PreviewConfig = field(default_factory=PreviewConfig)
    tui: TUIConfig = field(default_factory=TUIConfig)
    trace: TraceConfig = field(default_factory=TraceConfig)
    presets: dict[str, list[str]] = field(default_factory=dict)


def get_default_config_path() -> Path:
    """Return the default XDG configuration file path.

    Returns:
        Absolute path to ``~/.config/tail-cw/config.toml`` (or the platform
        equivalent). The containing directory is created when missing.
    """
    config_dir = Path(user_config_dir('tail-cw', ensure_exists=True))
    return config_dir / DEFAULT_CONFIG_FILENAME


def get_default_cache_dir() -> Path:
    """Return the default XDG cache directory.

    Returns:
        Absolute path to ``~/.cache/tail-cw`` (or the platform equivalent).
        The directory is created when missing.
    """
    return Path(user_cache_dir('tail-cw', opinion=False, ensure_exists=True))


def _to_cache_path(value: str | os.PathLike[str] | None) -> Path | None:
    if value is None:
        return None
    return _expand_path(value)


def _load_section(section: Any, factory: type[Any]) -> dict[str, Any]:
    if not isinstance(section, dict):
        return {}
    allowed_fields = {definition.name for definition in fields(factory)}
    return {key: section[key] for key in section if key in allowed_fields}


def _load_presets(section: Any) -> dict[str, list[str]]:
    match section:
        case None:
            return {}
        case dict():
            table: dict[str, Any] = section
        case _:
            msg = '[presets] must be a table mapping each name to a list of log groups'
            raise ValueError(msg)
    presets: dict[str, list[str]] = {}
    for name, value in table.items():
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            msg = f'Preset {name!r} must be a list of log group names'
            raise ValueError(msg)
        presets[name] = list(value)
    return presets


def load_config(config_path: Path | None = None) -> TailCWConfig:
    """Load configuration from a TOML file.

    Args:
        config_path: Optional path to a configuration file. When omitted the
            default path returned by :func:`get_default_config_path` is used.

    Returns:
        Parsed :class:`TailCWConfig` instance. Section defaults are applied for
        any missing keys or sections. When no configuration file exists, the
        defaults are returned.

    Raises:
        ValueError: If the configuration file cannot be parsed as TOML, or the
            ``[presets]`` table does not map each name to a list of strings.
        OSError: When reading the configuration file fails.

    Examples:
        >>> config = load_config(Path('config.toml'))
        >>> config.parquet.row_group_size
        100000
    """
    path = config_path or get_default_config_path()
    if not path.exists():
        config = TailCWConfig()
        if config.cache.cache_dir is None:
            config.cache.cache_dir = get_default_cache_dir()
        return config

    try:
        with path.open('rb') as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        msg = f'Invalid configuration file at {path}: {exc}'
        raise ValueError(msg) from exc
    except OSError:
        raise

    cache_kwargs = _load_section(data.get('cache'), CacheConfig)
    parquet_kwargs = _load_section(data.get('parquet'), ParquetConfig)
    preview_kwargs = _load_section(data.get('preview'), PreviewConfig)
    tui_kwargs = _load_section(data.get('tui'), TUIConfig)
    trace_kwargs = _load_section(data.get('trace'), TraceConfig)

    cache_dir_value = cache_kwargs.get('cache_dir')
    if cache_dir_value is not None:
        cache_kwargs['cache_dir'] = _to_cache_path(cache_dir_value)

    trace_fields = trace_kwargs.get('trace_id_fields')
    if trace_fields is not None:
        trace_kwargs['trace_id_fields'] = list(trace_fields)

    config = TailCWConfig(
        cache=CacheConfig(**cache_kwargs),
        parquet=ParquetConfig(**parquet_kwargs),
        preview=PreviewConfig(**preview_kwargs),
        tui=TUIConfig(**tui_kwargs),
        trace=TraceConfig(**trace_kwargs),
        presets=_load_presets(data.get('presets')),
    )

    if config.cache.cache_dir is None:
        config.cache.cache_dir = get_default_cache_dir()
    return config


def create_default_config_file(config_path: Path | None = None) -> Path:
    """Create a default configuration file with documented settings.

    The file is written atomically to avoid partial writes. Any required
    directories are created with restrictive permissions on POSIX systems.

    Args:
        config_path: Optional path specifying where to create the file. When
            omitted, :func:`get_default_config_path` is used.

    Returns:
        Path to the created configuration file.

    Raises:
        OSError: If the file cannot be written.

    Examples:
        >>> config_path = create_default_config_file()
        >>> config_path.exists()
        True
    """
    path = config_path or get_default_config_path()
    directory = path.parent
    try:
        directory.mkdir(parents=True, exist_ok=True)
        if sys.platform != 'win32':
            directory.chmod(0o700)

        template = (
            '# Tail CW configuration file\n'
            '# Customize settings and remove comments as needed.\n\n'
            '[cache]\n'
            '# cache_dir = "/path/to/cache"\n'
            'size_limit_mb = 1000\n'
            'default_ttl_seconds = 3600  # 1 hour\n'
            'eviction_policy = "least-recently-stored"\n\n'
            '[parquet]\n'
            'row_group_size = 100000\n'
            'compression_level = 3\n'
            'infer_schema_length = 1000\n\n'
            '[preview]\n'
            'sample_limit = 500\n'
            'window_seconds = 900  # 15 minutes\n'
            'ttl_seconds = 300  # 5 minutes\n\n'
            '[tui]\n'
            'chunk_threshold = 5000\n'
            'chunk_size = 1000\n'
            'initial_load_limit = 1000\n'
            'live_buffer_limit = 10000\n'
            'search_limit = 10000\n'
            'trace_limit = 100\n\n'
            '[trace]\n'
            'trace_id_fields = ["trace_id", "traceId", "x-trace-id"]\n\n'
            '[presets]\n'
            '# Reference a preset as @api wherever a log group pattern is accepted.\n'
            '# api = ["/aws/lambda/api-a", "/ecs/api-b"]\n'
        )

        temp_path = path.with_suffix('.tmp')
        temp_path.write_text(template, encoding='utf-8')
        temp_path.replace(path)
        if sys.platform != 'win32':
            path.chmod(0o600)
    except OSError as exc:
        msg = f'Failed to write configuration file at {path}'
        raise OSError(msg) from exc

    return path
