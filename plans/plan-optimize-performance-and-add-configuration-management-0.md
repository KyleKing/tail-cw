I have created the following plan after thorough exploration and analysis of the codebase. Follow the below plan verbatim. Trust the files and references. Do not re-verify what's written in the plan. Explore only when absolutely necessary. First implement all the proposed file changes and then I'll review all the changes together at the end.

### Observations

The project has a solid foundation with Parquet storage (storage.py), TUI with incremental loading (app.py), and dual-backend querying (engine.py). Current hardcoded values: row_group_size=100_000, compression_level=3, chunk_threshold=5000, chunk_size=1000, infer_schema_length=1000. The TUI already uses workers for search and incremental loading. No configuration system exists. The project uses corallium for file utilities, beartype for runtime type checking, and follows strict typing conventions. Tests use fix_test_cache fixture with TEST_TMP_CACHE path.

### Approach

Create a configuration system using TOML (stdlib `tomllib` for Python 3.11+) with XDG-compliant paths via `platformdirs`. Optimize Parquet writing with configurable row group size and compression. Add progress indicators using Textual's worker message pattern for AWS downloads and cache operations. Enhance DataTable with windowed/paged loading using existing incremental pattern. Leverage Polars' built-in parallelism by providing schema hints and optimizing `scan_ndjson` parameters. All configuration values will have sensible defaults and be optional parameters to maintain backward compatibility.

### Reasoning

I explored the codebase structure, examined current hardcoded values (row_group_size=100_000, compression_level=3, chunk_threshold=5000, chunk_size=1000), researched TOML configuration best practices with XDG paths and platformdirs, studied Textual's progress indicator patterns (LoadingIndicator, ProgressBar, worker messages), investigated Polars streaming and parallel JSONL parsing capabilities, and confirmed that the project uses Python >=3.11 with strict typing and functions-over-classes approach. The current implementation already has incremental loading for large datasets and Polars streaming, so optimizations will focus on making these configurable and adding progress feedback.

## Mermaid Diagram

sequenceDiagram participant User participant main participant Config participant LogTailApp participant LogCache participant Polars participant Worker

```
User->>main: Run tail-cw
main->>Config: load_config()
Config->>Config: Check ~/.config/tail-cw/config.toml
alt Config exists
    Config->>Config: Parse TOML with tomllib
    Config-->>main: Return TailCWConfig
else No config
    Config-->>main: Return defaults
end

main->>LogTailApp: Create app with config
LogTailApp->>LogTailApp: Store config values
LogTailApp-->>User: Display TUI

Note over User,Worker: User loads data from Parquet

User->>LogTailApp: set_parquet_source(path)
LogTailApp->>Worker: run_worker(load_data)
Worker->>LogTailApp: post_message(ProgressUpdate)
LogTailApp->>LogTailApp: Update status label

Worker->>Polars: scan_parquet with config.tui.initial_load_limit
Polars-->>Worker: Return events
Worker->>LogTailApp: Batch load with config.tui.chunk_size

loop For each chunk
    Worker->>LogTailApp: post_message(ProgressUpdate)
    LogTailApp->>LogTailApp: Update progress display
    Worker->>LogTailApp: Add rows to DataTable
end

Worker-->>LogTailApp: Loading complete
LogTailApp-->>User: Display loaded data

Note over User,LogCache: User triggers cache write

User->>LogCache: write(events, cache_key)
LogCache->>LogCache: Use config.parquet settings
LogCache->>Polars: scan_ndjson with config.parquet.infer_schema_length
Polars->>Polars: Parallel parsing (built-in)

loop During conversion
    Polars->>LogCache: Progress callback
    LogCache->>LogTailApp: post_message(ProgressUpdate)
    LogTailApp->>LogTailApp: Update status
end

LogCache->>Polars: sink_parquet with config.parquet.row_group_size
Polars-->>LogCache: Parquet file created
LogCache-->>User: Cache write complete
```

## Proposed File Changes

### tail_cw/config.py(NEW)

Create the configuration module with the following components:

1. **Import statements**: Import `from __future__ import annotations`, `dataclasses.dataclass`, `dataclasses.field`, `pathlib.Path`, `sys`, `platformdirs` for XDG paths.

1. **Add platformdirs dependency**: Note that `platformdirs` needs to be added to pyproject.toml dependencies (minimal, widely-used library for cross-platform directory resolution).

1. **CacheConfig dataclass**: Define configuration for cache behavior:

    - `cache_dir: Path | None = None` - Cache directory (None = use XDG default)
    - `size_limit_mb: int = 1000` - Maximum cache size in MB
    - `default_ttl_seconds: int | None = None` - Default TTL (None = no expiration)
    - `eviction_policy: str = 'least-recently-stored'` - DiskCache eviction policy
    - Add docstring explaining each field

1. **ParquetConfig dataclass**: Define Parquet writing configuration:

    - `row_group_size: int = 100_000` - Rows per row group (balance between query performance and memory)
    - `compression_level: int = 3` - ZSTD compression level (1-22, 3 is balanced)
    - `infer_schema_length: int = 1000` - Number of rows for schema inference
    - Add docstring with performance notes

1. **TUIConfig dataclass**: Define TUI behavior configuration:

    - `chunk_threshold: int = 5000` - Threshold for incremental loading
    - `chunk_size: int = 1000` - Events per chunk for incremental loading
    - `initial_load_limit: int = 1000` - Initial load limit from Parquet
    - `search_limit: int = 10000` - Maximum search results
    - `trace_limit: int = 100` - Maximum traces to load
    - Add docstring

1. **TraceConfig dataclass**: Define trace ID extraction configuration:

    - `trace_id_fields: list[str]` - Field names to search (default from DEFAULT_TRACE_ID_FIELDS)
    - Use `field(default_factory=...)` for mutable default
    - Add docstring

1. **TailCWConfig dataclass**: Main configuration container:

    - `cache: CacheConfig = field(default_factory=CacheConfig)` - Cache settings
    - `parquet: ParquetConfig = field(default_factory=ParquetConfig)` - Parquet settings
    - `tui: TUIConfig = field(default_factory=TUIConfig)` - TUI settings
    - `trace: TraceConfig = field(default_factory=TraceConfig)` - Trace settings
    - Add comprehensive docstring with example TOML structure

1. **Function `get_default_config_path`**: Get XDG-compliant config file path:

    - Returns: `Path` - Path to config file (e.g., ~/.config/tail-cw/config.toml)
    - Implementation:
        - Use `platformdirs.user_config_dir('tail-cw', ensure_exists=True)` to get config directory
        - Return `config_dir / 'config.toml'`
    - Add docstring explaining XDG compliance

1. **Function `get_default_cache_dir`**: Get XDG-compliant cache directory:

    - Returns: `Path` - Path to cache directory (e.g., ~/.cache/tail-cw)
    - Implementation:
        - Use `platformdirs.user_cache_dir('tail-cw', ensure_exists=True)`
    - Add docstring

1. **Function `load_config`**: Load configuration from TOML file:

    - Parameters:
        - `config_path: Path | None = None` - Path to config file (None = use default)
    - Returns: `TailCWConfig` - Loaded configuration with defaults
    - Implementation:
        - If config_path is None, use `get_default_config_path()`
        - If file doesn't exist, return default TailCWConfig instance
        - Read file in binary mode and parse with `tomllib.load()` (Python 3.11+)
        - Create TailCWConfig from parsed dict, using nested dataclass construction
        - Handle missing sections gracefully (use defaults)
        - Set cache_dir to XDG default if not specified in config
    - Add comprehensive docstring with example TOML structure
    - Add error handling for invalid TOML with clear error messages

1. **Function `create_default_config_file`**: Create a default config file with comments:

    - Parameters:
        - `config_path: Path | None = None` - Path to config file (None = use default)
    - Returns: `Path` - Path to created config file
    - Implementation:
        - Get config path (default if None)
        - Create parent directories with mode 0o700
        - Write a well-commented TOML template with all sections and defaults
        - Use atomic write pattern (write to temp, then replace)
    - Add docstring

1. **Example TOML structure** (to document in docstrings):

    ```toml
    [cache]
    # Cache directory (default: ~/.cache/tail-cw)
    # cache_dir = "/path/to/cache"
    size_limit_mb = 1000
    default_ttl_seconds = 3600                # 1 hour
    eviction_policy = "least-recently-stored"

    [parquet]
    row_group_size = 100000
    compression_level = 3
    infer_schema_length = 1000

    [tui]
    chunk_threshold = 5000
    chunk_size = 1000
    initial_load_limit = 1000
    search_limit = 10000
    trace_limit = 100

    [trace]
    trace_id_fields = ["trace_id", "traceId", "x-trace-id"]
    ```

1. **Type hints**: Ensure all functions have complete type hints compatible with mypy strict mode.

1. **Docstrings**: Follow Google docstring convention with Args, Returns, Examples sections.

1. **Error handling**: Raise specific exceptions (ValueError for invalid config, OSError for file errors) with clear messages.

### pyproject.toml(MODIFY)

Add `platformdirs` to the project dependencies:

1. In the `[project]` section, add to the `dependencies` list:

    - `platformdirs>=4.0.0` - For XDG-compliant directory resolution

1. Place it after `corallium>=2.0.1` in alphabetical order.

1. No changes needed to dependency-groups since this is a core dependency used by the config module.

Note: `platformdirs` is a minimal, widely-used library (used by pip, virtualenv, etc.) with no dependencies of its own.

### tail_cw/cache/storage.py(MODIFY)

Update cache storage to accept configuration and support progress callbacks:

1. **Add imports**: Import `collections.abc.Callable` for progress callback type.

1. **Define ProgressCallback type alias**: Add near top of file:

    - `ProgressCallback = Callable[[int, int, str], None]` - (current, total, status_message)
    - Add docstring explaining the callback signature

1. **Update `write_log_events_to_parquet` signature**: Add optional parameters:

    - `row_group_size: int = 100_000` - Make configurable (currently hardcoded)
    - `infer_schema_length: int = 1000` - Make configurable (currently hardcoded)
    - `progress_callback: ProgressCallback | None = None` - Optional progress reporting
    - Keep `compression_level` parameter as-is

1. **Update `write_log_events_to_parquet` implementation**:

    - After writing NDJSON, call progress_callback if provided: `progress_callback(total_events, total_events, 'Converting to Parquet...')`
    - Use the new `row_group_size` and `infer_schema_length` parameters in `sink_parquet` call
    - Update docstring to document new parameters

1. **Update `_log_events_to_ndjson_file` signature**: Add progress callback:

    - `progress_callback: ProgressCallback | None = None`

1. **Update `_log_events_to_ndjson_file` implementation**:

    - Add progress reporting every 1000 events:
        - `if progress_callback and total_events % 1000 == 0: progress_callback(total_events, -1, 'Parsing JSONL...')`
    - Note: total is -1 (unknown) since we're iterating

1. **Update `LogCache.write` method**: Accept config parameters:

    - Add optional parameters matching `write_log_events_to_parquet`
    - Pass through to `write_log_events_to_parquet`
    - Update docstring

1. **Update `LogCache.__init__` signature**: Accept config values:

    - Add `row_group_size: int = 100_000` parameter
    - Add `infer_schema_length: int = 1000` parameter
    - Store as instance attributes: `self._row_group_size`, `self._infer_schema_length`
    - Update docstring

1. **Update `LogCache.write` to use instance config**: Pass `self._row_group_size` and `self._infer_schema_length` to `write_log_events_to_parquet` if not explicitly provided.

1. **Backward compatibility**: All new parameters are optional with sensible defaults, so existing code continues to work.

1. **Type hints**: Update all signatures with new parameters.

1. **Docstrings**: Update all affected docstrings with new parameter descriptions and examples.

### tail_cw/aws/client.py(MODIFY)

Add progress callback support to AWS log fetching:

1. **Add import**: Import `collections.abc.Callable` for callback type.

1. **Define ProgressCallback type alias**: Add near top of file:

    - `ProgressCallback = Callable[[int, str], None]` - (events_fetched, status_message)
    - Add docstring explaining the callback signature
    - Note: Different signature from cache module since we don't know total events upfront

1. **Update `fetch_log_events` signature**: Add optional parameter:

    - `progress_callback: ProgressCallback | None = None` - Optional progress reporting

1. **Update `fetch_log_events` implementation**:

    - Track event count: `event_count = 0`
    - After yielding each event, increment counter and report progress every 100 events:
        - `event_count += 1`
        - `if progress_callback and event_count % 100 == 0: progress_callback(event_count, f'Fetched {event_count} events...')`
    - Update docstring to document new parameter

1. **Backward compatibility**: The progress_callback parameter is optional, so existing code continues to work.

1. **Type hints**: Update signature with new parameter.

1. **Docstrings**: Update docstring with parameter description and example usage.

### tail_cw/tui/app.py(MODIFY)

Integrate configuration and add progress indicators:

1. **Add imports**:

    - `from tail_cw.config import TailCWConfig, load_config`
    - `from textual.message import Message`
    - `from dataclasses import dataclass`

1. **Define ProgressUpdate message**: Add custom message class for progress updates:

    - `@dataclass` decorator
    - `class ProgressUpdate(Message):`
        - `current: int` - Current progress value
        - `total: int` - Total value (-1 if unknown)
        - `status: str` - Status message
    - Add docstring

1. **Update `LogTailApp.__init__` signature**: Add config parameter:

    - `config: TailCWConfig | None = None` - Configuration (None = load default)
    - Store as `self._config: TailCWConfig`
    - If config is None, call `load_config()` to get defaults
    - Update trace_id_fields default to use `config.trace.trace_id_fields`

1. **Update `_load_log_events` method**: Use config values:

    - Replace hardcoded `chunk_threshold = 5000` with `self._config.tui.chunk_threshold`
    - Replace hardcoded `chunk_size = 1000` with `self._config.tui.chunk_size`

1. **Update `set_parquet_source` method**: Use config values:

    - Replace hardcoded `limit=1000` with `self._config.tui.initial_load_limit`

1. **Update `_execute_search_query` method**: Use config values:

    - Replace hardcoded `limit=10000` with `self._config.tui.search_limit`

1. **Update `action_toggle_trace_view` method**: Use config values:

    - Replace hardcoded `trace_limit = 100` with `self._config.tui.trace_limit`

1. **Add progress indicator support**: Add method to handle progress messages:

    - `def on_progress_update(self, message: ProgressUpdate) -> None:`
        - Update status label with progress information
        - Format message based on whether total is known: `f'{message.status} ({message.current}/{message.total})'` or `f'{message.status} ({message.current} events)'`
    - Add docstring

1. **Add helper method for posting progress**: Add convenience method:

    - `def _post_progress(self, current: int, total: int, status: str) -> None:`
        - `self.post_message(ProgressUpdate(current, total, status))`
    - Add docstring

1. **Update incremental loading to show progress**: In `_load_events_incrementally`:

    - Replace direct status updates with progress messages
    - Use `self._post_progress(end_idx, total, 'Loading events')`

1. **Add loading indicator for table**: In `_load_log_events`:

    - Set `self._table.loading = True` at start (already done)
    - Ensure it's set to False after loading completes

1. **Update docstrings**: Update class and method docstrings to mention configuration support.

1. **Backward compatibility**: Config parameter is optional with sensible defaults loaded automatically.

1. **Type hints**: Update all signatures with new parameters.

### tail_cw/__main__.py(MODIFY)

Update entry point to load configuration:

1. **Add import**: `from tail_cw.config import load_config`

1. **Update `main` function**:

    - Load configuration at startup: `config = load_config()`
    - Pass config to LogTailApp: `app = LogTailApp(config=config)`
    - Update docstring to mention configuration loading

1. **Add comment**: Note that CLI argument parsing for config path override will be added in future (keep TODO comment).

1. **Error handling**: Wrap config loading in try-except to catch and report config errors:

    - Catch `ValueError` for invalid config
    - Catch `OSError` for file errors
    - Print error to stderr and exit with code 1

1. **Update docstring**: Mention that configuration is loaded from XDG-compliant path.

### tail_cw/config/__init__.py(NEW)

Create package initialization file to export the public config API:

1. Add module docstring explaining the configuration system.

1. Add imports:

    - `from tail_cw.config.config import TailCWConfig, CacheConfig, ParquetConfig, TUIConfig, TraceConfig, load_config, get_default_config_path, get_default_cache_dir, create_default_config_file`

1. Define `__all__` list:

    - `['TailCWConfig', 'CacheConfig', 'ParquetConfig', 'TUIConfig', 'TraceConfig', 'load_config', 'get_default_config_path', 'get_default_cache_dir', 'create_default_config_file']`

Note: The actual implementation will be in `tail_cw/config/config.py` to keep the module organized.

### tail_cw/config(NEW)

Create the config subdirectory under tail_cw/ to house configuration-related modules.

### tail_cw/config/config.py(NEW)

This is the actual implementation file for the configuration system. The content is the same as described in the first file change for `/Users/kyleking/Developer/kyleking/tail-cw/tail_cw/config.py`, but placed in the proper module structure as `tail_cw/config/config.py`.

All implementation details remain the same: dataclasses for configuration sections, TOML loading with tomllib, XDG path resolution with platformdirs, default config file creation, and comprehensive error handling.

### tests/test_config.py(NEW)

References:

- tests/conftest.py

Create comprehensive unit tests for the configuration module:

1. **Import statements**: Import `pytest`, `Path` from pathlib, `tempfile`, all config classes and functions from `tail_cw.config`.

1. **Test `test_default_config_values`**: Test default configuration:

    - Create `TailCWConfig()` with no arguments
    - Assert all default values match expected (cache size 1000MB, compression level 3, etc.)
    - Verify nested dataclasses are initialized

1. **Test `test_get_default_config_path`**: Test XDG config path:

    - Call `get_default_config_path()`
    - Assert path contains 'tail-cw' and 'config.toml'
    - Assert path is absolute

1. **Test `test_get_default_cache_dir`**: Test XDG cache directory:

    - Call `get_default_cache_dir()`
    - Assert path contains 'tail-cw'
    - Assert path is absolute

1. **Test `test_load_config_nonexistent_file`**: Test loading when file doesn't exist:

    - Call `load_config(Path('/nonexistent/config.toml'))`
    - Assert returns default TailCWConfig
    - Assert no errors raised

1. **Test `test_load_config_valid_toml`**: Test loading valid config:

    - Create temporary TOML file with custom values
    - Call `load_config(temp_path)`
    - Assert loaded values match TOML content
    - Test all sections (cache, parquet, tui, trace)

1. **Test `test_load_config_partial_toml`**: Test loading with missing sections:

    - Create TOML with only [cache] section
    - Call `load_config(temp_path)`
    - Assert cache values are loaded
    - Assert other sections use defaults

1. **Test `test_load_config_invalid_toml`**: Test error handling:

    - Create file with invalid TOML syntax
    - Call `load_config(temp_path)`
    - Assert raises appropriate exception (ValueError or tomllib.TOMLDecodeError)

1. **Test `test_create_default_config_file`**: Test config file creation:

    - Use temporary directory
    - Call `create_default_config_file(temp_path)`
    - Assert file exists
    - Assert file is valid TOML
    - Assert file contains all sections with comments

1. **Test `test_create_default_config_file_atomic`**: Test atomic write:

    - Create config file
    - Verify no .tmp files left behind

1. **Test `test_cache_config_dataclass`**: Test CacheConfig:

    - Create with custom values
    - Assert all fields accessible
    - Test with None values

1. **Test `test_parquet_config_dataclass`**: Test ParquetConfig:

    - Create with custom values
    - Assert all fields accessible
    - Test boundary values (compression 1-22)

1. **Test `test_tui_config_dataclass`**: Test TUIConfig:

    - Create with custom values
    - Assert all fields accessible

1. **Test `test_trace_config_dataclass`**: Test TraceConfig:

    - Create with custom trace_id_fields
    - Assert list is mutable
    - Test default factory

1. **Test `test_config_integration`**: Test full config workflow:

    - Create config file
    - Load config
    - Verify all values
    - Modify and reload

1. **Test `test_config_with_custom_cache_dir`**: Test custom cache directory:

    - Create TOML with cache_dir specified
    - Load config
    - Assert cache_dir is set correctly

1. **Test `test_config_xdg_compliance`**: Test XDG path behavior:

    - Verify paths follow XDG spec
    - Test with different XDG env vars (if possible)

1. **Fixtures**: Use `tmp_path` fixture from pytest for temporary files.

1. **Test organization**: Group related tests with descriptive names.

1. **Follow project conventions**: No type annotations on test functions, use assert statements.

### tests/test_cache.py(MODIFY)

Add tests for new cache configuration and progress callback features:

1. **Test `test_write_log_events_to_parquet_custom_row_group_size`**: Test custom row group size:

    - Create events
    - Write with `row_group_size=50_000`
    - Verify Parquet file is created
    - Read back and verify data integrity

1. **Test `test_write_log_events_to_parquet_custom_infer_schema_length`**: Test custom schema inference:

    - Create events with varied schema
    - Write with `infer_schema_length=100`
    - Verify Parquet file is created

1. **Test `test_write_log_events_to_parquet_with_progress_callback`**: Test progress reporting:

    - Create progress tracking list
    - Define callback that appends to list
    - Write events with callback
    - Assert callback was called with progress updates
    - Verify progress values are reasonable

1. **Test `test_log_cache_with_custom_config`**: Test LogCache with config:

    - Create LogCache with custom row_group_size and infer_schema_length
    - Write events
    - Verify Parquet file uses custom settings (may need to inspect metadata)

1. **Test `test_log_cache_write_with_progress`**: Test LogCache progress callback:

    - Create LogCache
    - Define progress callback
    - Write events with callback
    - Assert callback was called

1. **Update existing tests**: Ensure existing tests still pass with new optional parameters.

1. **Follow project conventions**: No type annotations on test functions, use assert statements.

### tests/test_aws_client.py(MODIFY)

Add tests for progress callback feature:

1. **Test `test_fetch_log_events_with_progress_callback`**: Test progress reporting:

    - Create stubbed client with multiple pages of events
    - Define progress callback that tracks calls
    - Call `fetch_log_events` with callback
    - Consume iterator
    - Assert callback was called multiple times
    - Verify progress values increase monotonically

1. **Test `test_fetch_log_events_progress_callback_frequency`**: Test callback frequency:

    - Create events (e.g., 250 events)
    - Track callback calls
    - Verify callback is called approximately every 100 events

1. **Test `test_fetch_log_events_without_progress_callback`**: Test backward compatibility:

    - Call `fetch_log_events` without callback parameter
    - Assert works as before (no errors)

1. **Update existing tests**: Ensure existing tests still pass with new optional parameter.

1. **Follow project conventions**: No type annotations on test functions, use assert statements.

### tests/test_tui_app.py(MODIFY)

Add tests for configuration integration and progress indicators:

1. **Test `test_app_with_custom_config`**: Test app with custom configuration:

    - Create TailCWConfig with custom values
    - Create LogTailApp with config
    - Verify app uses config values (check chunk_threshold, etc.)

1. **Test `test_app_loads_default_config`**: Test default config loading:

    - Create LogTailApp without config parameter
    - Verify app has valid config
    - Verify config has default values

1. **Test `test_app_progress_update_message`**: Test progress message handling:

    - Create app
    - Run with Pilot
    - Post ProgressUpdate message
    - Verify status label is updated

1. **Test `test_app_incremental_loading_with_progress`**: Test progress during loading:

    - Create app with large dataset (>5000 events)
    - Run with Pilot
    - Verify status updates show progress

1. **Test `test_app_uses_config_chunk_threshold`**: Test config-driven chunking:

    - Create config with custom chunk_threshold
    - Create app with config
    - Load events
    - Verify chunking behavior matches config

1. **Test `test_app_uses_config_search_limit`**: Test config-driven search limit:

    - Create config with custom search_limit
    - Create app with Parquet source
    - Perform search
    - Verify results respect limit

1. **Update existing tests**: Ensure existing tests still pass with config changes.

1. **Follow project conventions**: No type annotations on test functions, use assert statements.

### tests/test_main.py(MODIFY)

Add tests for configuration loading in main entry point:

1. **Test `test_main_loads_config`**: Test config loading:

    - Mock `load_config` to return custom config
    - Mock `LogTailApp` to capture config parameter
    - Call `main()`
    - Assert `load_config` was called
    - Assert `LogTailApp` received config

1. **Test `test_main_handles_config_error`**: Test config error handling:

    - Mock `load_config` to raise ValueError
    - Capture stderr
    - Call `main()`
    - Assert returns error code 1
    - Assert error message in stderr

1. **Update existing tests**: Ensure existing tests still pass with config loading.

1. **Follow project conventions**: No type annotations on test functions, use assert statements.

### docs/docs/CONFIGURATION.md(NEW)

Create user documentation for configuration:

1. **Title**: `# Configuration Guide`

1. **Overview section**: Explain the configuration system:

    - TOML-based configuration
    - XDG-compliant paths
    - Default values and customization

1. **Configuration File Location section**: Document where config is stored:

    - Linux/macOS: `~/.config/tail-cw/config.toml`
    - Windows: `%APPDATA%\tail-cw\config.toml`
    - How to find the path programmatically

1. **Configuration Sections section**: Document each section:

    - `[cache]` - Cache behavior (directory, size, TTL, eviction)
    - `[parquet]` - Parquet storage settings (row groups, compression)
    - `[tui]` - TUI behavior (chunk sizes, limits)
    - `[trace]` - Trace ID extraction (field names)

1. **Example Configuration section**: Provide complete example TOML with comments.

1. **Creating a Configuration File section**: Explain how to create default config:

    - Manual creation
    - Using Python API (if we add CLI command)

1. **Performance Tuning section**: Provide guidance on tuning settings:

    - Row group size for query performance
    - Compression level trade-offs
    - Chunk sizes for UI responsiveness
    - Cache size and TTL considerations

1. **Troubleshooting section**: Common issues and solutions:

    - Invalid TOML syntax
    - Permission errors
    - Cache directory issues

1. **Advanced Topics section**:

    - Environment variable overrides (future)
    - Multiple configuration profiles (future)

1. **Use markdown formatting** for readability.
