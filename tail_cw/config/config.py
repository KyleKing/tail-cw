"""Configuration loading and defaults for tail_cw.

This module exposes dataclasses describing the configurable aspects of the
application along with helpers for loading user-supplied TOML files from
XDG-compliant locations. Consumers can customize cache behaviour, TUI
pagination, and trace extraction without modifying source code.
"""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Final

from platformdirs import user_cache_dir, user_config_dir

from tail_cw.concurrency import DEFAULT_FETCH_WORKERS
from tail_cw.query.trace import DEFAULT_TRACE_ID_FIELDS

DEFAULT_CONFIG_FILENAME: Final = 'config.toml'


def _expand_path(value: str | os.PathLike[str]) -> Path:
    """Expand user and environment variables in a path-like value.

    Returns:
        Absolute, expanded path for the provided value.
    """
    return Path(os.path.expandvars(Path(str(value)).expanduser())).resolve()


@dataclass(slots=True)
class AwsConfig:
    """Account defaults, so the most-typed flags need not be typed.

    Attributes:
        profile: AWS profile used when ``--profile`` is absent. A preset naming
            its own profile outranks this; ``--profile`` outranks both.
        region: AWS region used when ``--region`` is absent.
    """

    profile: str | None = None
    region: str | None = None


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
class FetchConfig:
    """How much of one window tail-cw asks CloudWatch for at once.

    Attributes:
        max_concurrent_segments: Segment fetches in flight at once, counted
            across every log group in one command. ``FilterLogEvents`` paginates
            serially, so a window split across concurrent segments is several
            times faster; the default matches the fetch pool's width because each
            in-flight segment holds one of its threads until it finishes.
    """

    max_concurrent_segments: int = DEFAULT_FETCH_WORKERS


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
        theme: Textual theme name, such as ``catppuccin-mocha``, ``gruvbox``, or
            ``ansi-dark``. ``ansi-dark`` and ``ansi-light`` are the two that use the
            terminal's own sixteen colours rather than fixed ones, so they are the
            answer for a terminal whose palette you have already chosen.
    """

    chunk_threshold: int = 5000
    chunk_size: int = 1000
    initial_load_limit: int = 1000
    live_buffer_limit: int = 10_000
    search_limit: int = 10_000
    trace_limit: int = 100
    theme: str = 'catppuccin-mocha'


@dataclass(slots=True)
class InsightsConfig:
    """Guards on the one query path that bills.

    Attributes:
        confirm_above_gb: Estimated gigabytes a query may scan before it needs
            an explicit confirmation.
    """

    confirm_above_gb: float = 1.0


@dataclass(slots=True)
class MessageConfig:
    """Which record fields carry the human-readable phrase in the log table.

    Attributes:
        phrase_fields: Keys tried in order for the sentence a person reads. The
            first present wins; the rest of the record renders as ``key=value``.
        hidden_fields: Keys dropped from the tabulated remainder, for identifiers
            the table's own columns already carry.
    """

    phrase_fields: list[str] = field(default_factory=lambda: ['event', 'message', 'msg', 'log', 'text'])
    hidden_fields: list[str] = field(default_factory=lambda: ['timestamp', 'time', 'asctime', 'level', 'levelname'])


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

        [filters]
        errors = "level:error OR level:critical"

    Attributes:
        aws: Default profile and region.
        cache: Cache persistence configuration.
        fetch: How many segment fetches run at once.
        insights: Guards on billed Logs Insights queries.
        message: Which record fields the log table reads as the phrase.
        preview: Log group preview sampling configuration.
        tui: TUI incremental loading configuration.
        trace: Trace extraction configuration.
        presets: Named log group sets, referenced as ``@name`` wherever a log
            group pattern is accepted.
        preset_profiles: The AWS profile a preset names, for the presets that
            name one.
        filters: Named filter expressions, referenced as ``@name`` wherever a
            filter is accepted, extending the same convention as ``presets``.
    """

    aws: AwsConfig = field(default_factory=AwsConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)
    fetch: FetchConfig = field(default_factory=FetchConfig)
    insights: InsightsConfig = field(default_factory=InsightsConfig)
    message: MessageConfig = field(default_factory=MessageConfig)
    preview: PreviewConfig = field(default_factory=PreviewConfig)
    tui: TUIConfig = field(default_factory=TUIConfig)
    trace: TraceConfig = field(default_factory=TraceConfig)
    presets: dict[str, list[str]] = field(default_factory=dict)
    preset_profiles: dict[str, str] = field(default_factory=dict)
    filters: dict[str, str] = field(default_factory=dict)


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


def _load_presets(section: Any) -> tuple[dict[str, list[str]], dict[str, str]]:
    """Read ``[presets]`` in either of its two shapes.

    A preset is a bare list of log groups, or a table with ``groups`` and an
    optional ``profile`` for a set that lives in another account.

    Returns:
        The groups per preset, and the profile for those that name one.

    Raises:
        ValueError: If the section, a preset, or a preset's profile is malformed.
    """
    match section:
        case None:
            return {}, {}
        case dict():
            table: dict[str, Any] = section
        case _:
            msg = '[presets] must be a table mapping each name to a list of log groups'
            raise ValueError(msg)
    presets: dict[str, list[str]] = {}
    profiles: dict[str, str] = {}
    for name, value in table.items():
        groups, profile = _load_one_preset(name, value)
        presets[name] = groups
        if profile is not None:
            profiles[name] = profile
    return presets, profiles


def _load_one_preset(name: str, value: Any) -> tuple[list[str], str | None]:
    groups = value.get('groups') if isinstance(value, dict) else value
    if not isinstance(groups, list) or any(not isinstance(item, str) for item in groups):
        msg = f'Preset {name!r} must be a list of log group names, or a table with a groups list'
        raise ValueError(msg)
    profile = value.get('profile') if isinstance(value, dict) else None
    if profile is not None and (not isinstance(profile, str) or not profile.strip()):
        msg = f'Preset {name!r} has a profile that is not a name'
        raise ValueError(msg)
    return list(groups), profile


def _load_named_filters(section: Any) -> dict[str, str]:
    match section:
        case None:
            return {}
        case dict():
            table: dict[str, Any] = section
        case _:
            msg = '[filters] must be a table mapping each name to a filter expression'
            raise ValueError(msg)
    filters: dict[str, str] = {}
    for name, value in table.items():
        if not isinstance(value, str) or not value.strip():
            msg = f'Filter {name!r} must be a non-empty filter expression'
            raise ValueError(msg)
        filters[name] = value
    return filters


_SECTIONS: Final[dict[str, Any]] = {
    'aws': AwsConfig,
    'cache': CacheConfig,
    'fetch': FetchConfig,
    'insights': InsightsConfig,
    'message': MessageConfig,
    'preview': PreviewConfig,
    'tui': TUIConfig,
    'trace': TraceConfig,
}
"""TOML section name to its dataclass. Each key is also the matching :class:`TailCWConfig` field."""


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
        >>> config.cache.size_limit_mb
        1000
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

    sections = {name: _load_section(data.get(name), factory) for name, factory in _SECTIONS.items()}
    if (cache_dir_value := sections['cache'].get('cache_dir')) is not None:
        sections['cache']['cache_dir'] = _to_cache_path(cache_dir_value)
    if (trace_fields := sections['trace'].get('trace_id_fields')) is not None:
        sections['trace']['trace_id_fields'] = list(trace_fields)

    presets, preset_profiles = _load_presets(data.get('presets'))
    config = TailCWConfig(
        **{name: _SECTIONS[name](**kwargs) for name, kwargs in sections.items()},
        presets=presets,
        preset_profiles=preset_profiles,
        filters=_load_named_filters(data.get('filters')),
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
            '[aws]\n'
            '# Used when --profile and --region are absent.\n'
            '# profile = "read-prod"\n'
            '# region = "us-east-1"\n\n'
            '[cache]\n'
            '# cache_dir = "/path/to/cache"\n'
            'size_limit_mb = 1000\n'
            'default_ttl_seconds = 3600  # 1 hour\n'
            'eviction_policy = "least-recently-stored"\n\n'
            '[fetch]\n'
            '# Segment fetches in flight at once, across every log group.\n'
            f'max_concurrent_segments = {DEFAULT_FETCH_WORKERS}\n\n'
            '[insights]\n'
            '# Estimated GB a query may scan before it asks for confirmation.\n'
            'confirm_above_gb = 1.0\n\n'
            '[message]\n'
            '# phrase_fields = ["event", "message", "msg"]\n\n'
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
            'trace_limit = 100\n'
            '# Any Textual theme: catppuccin-mocha, catppuccin-latte, gruvbox, nord,\n'
            '# tokyo-night, dracula, solarized-dark. ansi-dark and ansi-light use the\n'
            "# terminal's own sixteen colours instead of fixed ones.\n"
            'theme = "catppuccin-mocha"\n\n'
            '[trace]\n'
            'trace_id_fields = ["trace_id", "traceId", "x-trace-id"]\n\n'
            '[presets]\n'
            '# Reference a preset as @api wherever a log group pattern is accepted.\n'
            '# api = ["/aws/lambda/api-a", "/ecs/api-b"]\n'
            '# A preset in another account carries its own profile:\n'
            '# [presets.billing]\n'
            '# groups = ["/aws/lambda/billing"]\n'
            '# profile = "read-billing"\n\n'
            '[filters]\n'
            '# Reference a filter as @errors wherever a filter is accepted.\n'
            '# errors = "level:error OR level:critical"\n'
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
