# Configuration Guide

This guide explains how to customise tail-cw using its TOML-based configuration system. The application automatically loads configuration from an XDG-compliant location on start-up, allowing you to tune performance and user experience without modifying source code.

## Overview

- Configuration files use standard TOML syntax.
- Settings are organised into sections that mirror the internal dataclasses.
- All keys are optional; unspecified values fall back to safe defaults.
- A helper API is available via `tail_cw.config` for programmatic access.

## Configuration File Location

| Platform      | Default path                    |
| ------------- | ------------------------------- |
| Linux / macOS | `~/.config/tail-cw/config.toml` |
| Windows       | `%APPDATA%\tail-cw\config.toml` |

Use `tail_cw.config.get_default_config_path()` to resolve the exact location on the current platform. The helper will create the containing directory if it does not exist.

Cache files created by the application are stored separately in the XDG cache directory (`~/.cache/tail-cw` on Linux/macOS, `%LOCALAPPDATA%\tail-cw` on Windows). Retrieve this location programmatically via `tail_cw.config.get_default_cache_dir()`.

## Configuration Sections

- `[cache]` controls cache storage limits and eviction behaviour.
- `[parquet]` tunes the Parquet writer used when materialising NDJSON logs.
- `[preview]` bounds the log group previews shown in the browser.
- `[tui]` exposes pagination and search limits for the Textual UI.
- `[trace]` lists the field names inspected when extracting distributed trace identifiers.

## Example Configuration

```toml
[cache]
# cache_dir = "/custom/cache/path"
size_limit_mb = 1024
default_ttl_seconds = 3600                # 1 hour
eviction_policy = "least-recently-stored"

[parquet]
row_group_size = 200000
compression_level = 5
infer_schema_length = 200

[preview]
sample_limit = 500   # events read per group preview
window_seconds = 900 # how far back a preview looks
ttl_seconds = 300    # how long a cached preview stays fresh

[tui]
chunk_threshold = 4000
chunk_size = 500
initial_load_limit = 1500
live_buffer_limit = 10000
search_limit = 20000
trace_limit = 150

[trace]
trace_id_fields = ["trace_id", "traceId", "context.trace_id"]
```

## Creating a Configuration File

1. Run `from tail_cw.config import create_default_config_file; create_default_config_file()` in a Python REPL to scaffold a documented template in the default location.
1. Alternatively, copy the example configuration above into the default path and adjust values as needed.

The helper performs an atomic write and sets restrictive permissions on POSIX systems to keep credentials and preferences private.

## Performance Tuning

- **Row group size**: larger values improve Parquet scan throughput at the cost of additional memory during writes. Reduce the value when writing on memory constrained machines.
- **Compression level**: higher ZSTD levels produce smaller files but increase CPU usage. Level 3 is a balanced default; try values between 1 and 6 when iterating.
- **Schema inference length**: increase when NDJSON payloads contain highly variable structures so late fields are discovered during conversion.
- **TUI chunk threshold / size**: lower thresholds trigger incremental loading sooner, which can help when working with deep scrollback buffers.
- **Live buffer limit**: bounds memory during `tail-cw tail` sessions; older events are evicted from the in-memory ring buffer once the limit is reached. Increase it for longer in-session scrollback, decrease it on memory constrained machines.
- **Search limit**: limit the number of rows collected from Parquet queries to keep the interface responsive when exploring large datasets.

## Troubleshooting

- **Invalid TOML**: errors indicate a parsing problem. Validate the file with a TOML linter or remove recent edits.
- **Permission errors**: ensure the user running tail-cw can read the config directory and write to the cache directory.
- **Cache directory missing**: override `cache_dir` in the `[cache]` section or verify that `get_default_cache_dir()` points to a writable location.
- **Group previews cost too many API calls**: each preview is one `FilterLogEvents` call per group, cached for `ttl_seconds`. Raise the TTL to walk a long list more cheaply, or lower `sample_limit` to read less per group.

## Advanced Topics

- **Environment overrides**: future releases may allow environment variables to override individual settings for temporary tuning.
- **Profiles**: support for multiple configuration profiles (e.g. per project) is under consideration. Until then, script switching by copying template files into place before launching tail-cw.
