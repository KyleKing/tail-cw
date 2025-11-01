"""Configuration helpers and dataclasses for tail_cw.

This module exposes the public configuration API, allowing callers to load
user-defined settings, discover default configuration locations, and scaffold
new configuration files. See `tail_cw.config.config` for the implementation
details.
"""

from tail_cw.config.config import (
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

__all__ = [
    'CacheConfig',
    'ParquetConfig',
    'TUIConfig',
    'TailCWConfig',
    'TraceConfig',
    'create_default_config_file',
    'get_default_cache_dir',
    'get_default_config_path',
    'load_config',
]
