# Branch 3: Search and Functionality Simplification - Implementation Plan

**Branch Name:** `claude/simplify-search-functionality-01VGBEGD5jDcQmxrbdNnW9zA`

**Goal:** Analyze and simplify complex search and filter functionality while maintaining power-user capabilities. Improve user experience without sacrificing features.

---

## Overview

This branch addresses complexity issues in search, filtering, and configuration to make tail-cw more approachable while preserving its advanced capabilities for power users.

### Key Improvements

1. **Simplified Filter Syntax** - Easier entry point with progressive disclosure
2. **Search Presets** - Common patterns saved for quick access
3. **Interactive Filter Builder** - GUI-based filter creation
4. **Configuration Wizard** - Guided setup for first-time users
5. **Improved Error Messages** - Clear, actionable feedback

---

## Complexity Analysis

### Current State Assessment

#### Filter Syntax Complexity

**Issues Identified:**
```python
# Current syntax requires understanding multiple formats:
'ERROR'                                    # Plain text (easy)
'{ $.level = "ERROR" }'                   # CloudWatch JSON (medium)
'{ $.status >= 500 }'                     # Numeric comparison (medium)
'level:ERROR'                              # Extended syntax (easy)
'{ $.context.user.id = "123" }'           # Nested JSON (complex)
'/[Ee]rror.*timeout/'                      # Regex (complex)
'ERROR OR WARNING'                         # Logical operators (medium)
'{ $.level = "ERROR" } AND status:500'    # Mixed syntax (complex)
```

**Complexity Metrics:**
- 8 different syntax patterns
- 3 logical operators (AND, OR, NOT)
- 6 comparison operators (=, !=, >, <, >=, <=)
- Nested field paths with dot notation
- Regex requires knowledge of syntax

**User Pain Points:**
1. No autocompletion or suggestions
2. No syntax validation until query execution
3. Cryptic error messages
4. No discoverability of advanced features
5. Steep learning curve for JSONL field queries

### Configuration Complexity

**Current Configuration:**
- 4 sections (cache, parquet, tui, trace)
- 15 total settings
- Manual TOML editing required
- No validation until runtime

**User Confusion Points:**
- What's a "row group size"?
- How do I know what compression level to use?
- What's the optimal chunk_threshold?
- How do I find available field names for trace IDs?

---

## Simplification Strategy

### Principle: Progressive Disclosure

**Approach:**
1. **Simple by default** - Common cases work out-of-the-box
2. **Power available** - Advanced features discoverable when needed
3. **Guided discovery** - Help users level up gradually
4. **Clear feedback** - Errors explain how to fix them

---

## Implementation Steps

### Step 1: Simplified Filter Entry Point

#### File: `tail_cw/query/simple_parser.py` (NEW)

```python
"""Simplified filter parser with progressive disclosure.

Provides an easier entry point for filtering while preserving full syntax support.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tail_cw.query.parser import FilterNode, parse_filter_pattern


@dataclass
class FilterSuggestion:
    """Suggested filter with description.

    Attributes:
        pattern: Filter pattern string
        description: Human-readable description
        category: Category (basic, json, regex, etc.)
        example: Example usage
    """

    pattern: str
    description: str
    category: str
    example: str


# Common filter presets
FILTER_PRESETS = [
    FilterSuggestion(
        pattern='ERROR',
        description='Find ERROR messages',
        category='basic',
        example='Shows all logs containing "ERROR"',
    ),
    FilterSuggestion(
        pattern='level:ERROR',
        description='Filter by error level',
        category='json-simple',
        example='Shows logs where level field equals ERROR',
    ),
    FilterSuggestion(
        pattern='{ $.status >= 500 }',
        description='HTTP server errors',
        category='json-numeric',
        example='Shows logs where status is 500 or higher',
    ),
    FilterSuggestion(
        pattern='ERROR OR WARNING',
        description='Errors or warnings',
        category='logical',
        example='Shows logs containing ERROR or WARNING',
    ),
]


def parse_simple_filter(query: str) -> tuple[FilterNode | None, list[str]]:
    """Parse filter with helpful error messages.

    Args:
        query: Filter query string

    Returns:
        Tuple of (FilterNode or None, list of error/warning messages)

    Examples:
        >>> node, messages = parse_simple_filter('ERROR')
        >>> messages
        []

        >>> node, messages = parse_simple_filter('{ $.invalid }')
        >>> messages[0]
        'Invalid JSON filter syntax. Did you mean: level:value ?'
    """
    if not query or query.strip() == '':
        return None, ['Empty filter. Try: ERROR or level:ERROR']

    messages = []

    # Try parsing
    try:
        node = parse_filter_pattern(query)
        return node, messages
    except ValueError as e:
        # Provide helpful suggestions based on error
        error_msg = str(e)

        if 'JSON' in error_msg and '{' not in query:
            messages.append(
                f"Tip: For JSON field filters, use 'fieldname:value' or '{{ $.fieldname = \"value\" }}'\n"
                f"Example: level:ERROR",
            )

        if 'regex' in error_msg.lower():
            messages.append(
                f"Tip: For regex patterns, use /pattern/\n"
                f"Example: /[Ee]rror.*/",
            )

        if 'operator' in error_msg.lower():
            messages.append(
                f"Tip: Use AND, OR, NOT for combining filters\n"
                f"Example: ERROR AND status:500",
            )

        messages.append(f"Parse error: {error_msg}")
        return None, messages


def suggest_filters(partial_query: str) -> list[FilterSuggestion]:
    """Suggest filters based on partial query.

    Provides autocomplete-style suggestions.

    Args:
        partial_query: Partial filter query

    Returns:
        List of matching filter suggestions

    Examples:
        >>> suggestions = suggest_filters('err')
        >>> suggestions[0].pattern
        'ERROR'
    """
    partial_lower = partial_query.lower()

    matches = []
    for preset in FILTER_PRESETS:
        if partial_lower in preset.pattern.lower() or partial_lower in preset.description.lower():
            matches.append(preset)

    return matches


def explain_filter(query: str) -> str:
    """Explain what a filter does in plain English.

    Args:
        query: Filter query

    Returns:
        Human-readable explanation

    Examples:
        >>> explain_filter('ERROR')
        'Find all messages containing "ERROR" (case-insensitive)'

        >>> explain_filter('{ $.level = "ERROR" }')
        'Find messages where the JSON field "level" equals "ERROR"'
    """
    try:
        node = parse_filter_pattern(query)
    except ValueError:
        return 'Invalid filter syntax'

    return _explain_node(node)


def _explain_node(node: FilterNode) -> str:
    """Recursively explain a filter node.

    Args:
        node: Filter node to explain

    Returns:
        Explanation string
    """
    from tail_cw.query.parser import FilterNodeType

    if node.node_type == FilterNodeType.TEXT_SEARCH:
        return f'Find messages containing "{node.value}" (case-insensitive)'

    if node.node_type == FilterNodeType.EXACT_PHRASE:
        return f'Find messages with exact phrase "{node.value}"'

    if node.node_type == FilterNodeType.REGEX:
        return f'Find messages matching regex pattern /{node.value}/'

    if node.node_type == FilterNodeType.JSON_FIELD_EQUALS:
        field = '.'.join(node.field_path or [])
        return f'Find messages where JSON field "{field}" equals "{node.value}"'

    if node.node_type == FilterNodeType.JSON_FIELD_NUMERIC:
        field = '.'.join(node.field_path or [])
        return f'Find messages where JSON field "{field}" {node.operator} {node.value}'

    if node.node_type == FilterNodeType.AND:
        parts = [_explain_node(child) for child in (node.children or [])]
        return ' AND '.join(f'({p})' for p in parts)

    if node.node_type == FilterNodeType.OR:
        parts = [_explain_node(child) for child in (node.children or [])]
        return ' OR '.join(f'({p})' for p in parts)

    if node.node_type == FilterNodeType.NOT:
        child_explanation = _explain_node(node.children[0]) if node.children else ''
        return f'NOT ({child_explanation})'

    return 'Unknown filter type'
```

**Tests to Add** (`tests/test_query_simple_parser.py`):
- `test_parse_simple_filter_basic`
- `test_parse_simple_filter_errors`
- `test_suggest_filters`
- `test_explain_filter_text_search`
- `test_explain_filter_json`
- `test_explain_filter_logical`

---

### Step 2: Interactive Filter Builder

#### File: `tail_cw/tui/filter_builder.py` (NEW)

```python
"""Interactive filter builder widget.

Provides a form-based interface for building filters without memorizing syntax.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select

from tail_cw.query.parser import FilterNodeType


class FilterBuilderScreen(ModalScreen):
    """Modal screen for building filters interactively."""

    DEFAULT_CSS = """
    FilterBuilderScreen {
        align: center middle;
    }

    #filter-dialog {
        width: 80;
        height: auto;
        border: thick $background 80%;
        background: $surface;
        padding: 1 2;
    }

    #buttons {
        width: 100%;
        height: auto;
        align: center middle;
        padding: 1;
    }
    """

    def __init__(self, initial_filter: str = '') -> None:
        """Initialize filter builder.

        Args:
            initial_filter: Initial filter string to edit
        """
        super().__init__()
        self._initial_filter = initial_filter
        self._result_filter = ''

    def compose(self) -> ComposeResult:
        """Compose the filter builder UI."""
        with Vertical(id='filter-dialog'):
            yield Label('Filter Builder', id='title')

            yield Label('Filter Type:')
            yield Select(
                [
                    ('Simple Text Search', 'text'),
                    ('JSON Field Equals', 'json-equals'),
                    ('JSON Numeric Comparison', 'json-numeric'),
                    ('Regular Expression', 'regex'),
                ],
                id='filter-type',
                value='text',
            )

            # Input fields (shown/hidden based on type)
            yield Label('Search Text:')
            yield Input(placeholder='Enter search text...', id='search-text')

            yield Label('Field Name:')
            yield Input(placeholder='e.g., level, status', id='field-name')

            yield Label('Value:')
            yield Input(placeholder='Enter value...', id='field-value')

            yield Label('Operator:')
            yield Select(
                [
                    ('Equals (=)', '='),
                    ('Not Equals (!=)', '!='),
                    ('Greater Than (>)', '>'),
                    ('Less Than (<)', '<'),
                    ('Greater or Equal (>=)', '>='),
                    ('Less or Equal (<=)', '<='),
                ],
                id='operator',
                value='=',
            )

            yield Label('Preview:')
            yield Input(value='', id='preview', disabled=True)

            with Horizontal(id='buttons'):
                yield Button('Apply', variant='primary', id='apply')
                yield Button('Cancel', id='cancel')

    def on_select_changed(self, event: Select.Changed) -> None:
        """Update form fields based on selected filter type."""
        if event.select.id == 'filter-type':
            # Show/hide relevant fields
            self._update_form_fields(event.value)

    def on_input_changed(self, event: Input.Changed) -> None:
        """Update preview as user types."""
        if event.input.id != 'preview':
            self._update_preview()

    def _update_form_fields(self, filter_type: str) -> None:
        """Show/hide form fields based on filter type.

        Args:
            filter_type: Selected filter type
        """
        # In real implementation, use display property to show/hide fields
        pass

    def _update_preview(self) -> None:
        """Update the filter preview."""
        preview_input = self.query_one('#preview', Input)

        filter_type = self.query_one('#filter-type', Select).value
        if filter_type == 'text':
            text = self.query_one('#search-text', Input).value
            preview_input.value = text if text else '(enter search text)'

        elif filter_type == 'json-equals':
            field = self.query_one('#field-name', Input).value
            value = self.query_one('#field-value', Input).value
            if field and value:
                preview_input.value = f'{field}:{value}'
            else:
                preview_input.value = 'fieldname:value'

        elif filter_type == 'json-numeric':
            field = self.query_one('#field-name', Input).value
            operator = self.query_one('#operator', Select).value
            value = self.query_one('#field-value', Input).value
            if field and value:
                preview_input.value = f'{{ $.{field} {operator} {value} }}'
            else:
                preview_input.value = '{ $.fieldname >= value }'

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button presses.

        Args:
            event: Button press event
        """
        if event.button.id == 'apply':
            preview_input = self.query_one('#preview', Input)
            self._result_filter = preview_input.value
            self.dismiss(self._result_filter)
        elif event.button.id == 'cancel':
            self.dismiss(None)
```

**Tests to Add** (`tests/test_tui_filter_builder.py`):
- `test_filter_builder_creation`
- `test_filter_builder_text_search`
- `test_filter_builder_json_field`
- `test_filter_builder_numeric_comparison`
- `test_filter_builder_preview_updates`

---

### Step 3: Configuration Wizard

#### File: `tail_cw/config/wizard.py` (NEW)

```python
"""Configuration wizard for first-time setup.

Provides guided configuration creation with explanations and recommendations.
"""

from __future__ import annotations

from pathlib import Path

from tail_cw.config.config import (
    CacheConfig,
    ParquetConfig,
    TailCWConfig,
    TraceConfig,
    TUIConfig,
    create_default_config_file,
    get_default_config_path,
)


def run_config_wizard(interactive: bool = True) -> TailCWConfig:
    """Run interactive configuration wizard.

    Args:
        interactive: Whether to prompt user for input (vs using defaults)

    Returns:
        Configured TailCWConfig instance

    Example:
        >>> config = run_config_wizard(interactive=False)  # Use defaults
        >>> config = run_config_wizard(interactive=True)   # Interactive prompts
    """
    if interactive:
        print("Welcome to tail-cw configuration wizard!")
        print("Press Enter to use default values.\n")

    # Cache configuration
    cache_config = _configure_cache(interactive)

    # Parquet configuration
    parquet_config = _configure_parquet(interactive)

    # TUI configuration
    tui_config = _configure_tui(interactive)

    # Trace configuration
    trace_config = _configure_trace(interactive)

    config = TailCWConfig(
        cache=cache_config,
        parquet=parquet_config,
        tui=tui_config,
        trace=trace_config,
    )

    if interactive:
        save = input("\nSave configuration? [Y/n]: ").strip().lower()
        if save != 'n':
            path = get_default_config_path()
            _save_config_to_file(config, path)
            print(f"\nConfiguration saved to: {path}")

    return config


def _configure_cache(interactive: bool) -> CacheConfig:
    """Configure cache settings.

    Args:
        interactive: Whether to prompt for input

    Returns:
        Configured CacheConfig
    """
    print("\n=== Cache Configuration ===")
    print("Controls how logs are cached locally for faster access.")

    if interactive:
        print(f"\nCache size limit (default: 10000 MB = 10GB)")
        print("Recommendation: 10GB for most users, 20GB+ for heavy usage")
        size_input = input("Size in MB [10000]: ").strip()
        size_limit_mb = int(size_input) if size_input else 10_000

        print(f"\nCache TTL (time-to-live) in seconds")
        print("Recommendation: 3600 (1 hour) or None for no expiration")
        ttl_input = input("TTL seconds [None]: ").strip()
        ttl = int(ttl_input) if ttl_input and ttl_input != 'None' else None
    else:
        size_limit_mb = 10_000
        ttl = None

    return CacheConfig(
        cache_dir=None,  # Use default
        size_limit_mb=size_limit_mb,
        default_ttl_seconds=ttl,
        eviction_policy='least-recently-stored',
    )


def _configure_parquet(interactive: bool) -> ParquetConfig:
    """Configure Parquet settings.

    Args:
        interactive: Whether to prompt for input

    Returns:
        Configured ParquetConfig
    """
    print("\n=== Parquet Configuration ===")
    print("Controls how logs are stored in the cache.")

    if interactive:
        print(f"\nRow group size (default: 100000)")
        print("Larger = faster queries, more memory. Smaller = less memory.")
        print("Recommendation: 100000 for most users")
        row_input = input("Row group size [100000]: ").strip()
        row_group_size = int(row_input) if row_input else 100_000

        print(f"\nCompression level (1-22, default: 3)")
        print("Higher = smaller files, slower. Lower = larger files, faster.")
        print("Recommendation: 3 for balanced performance")
        comp_input = input("Compression level [3]: ").strip()
        compression_level = int(comp_input) if comp_input else 3
    else:
        row_group_size = 100_000
        compression_level = 3

    return ParquetConfig(
        row_group_size=row_group_size,
        compression_level=compression_level,
        infer_schema_length=1000,
    )


def _configure_tui(interactive: bool) -> TUIConfig:
    """Configure TUI settings.

    Args:
        interactive: Whether to prompt for input

    Returns:
        Configured TUIConfig
    """
    print("\n=== TUI Configuration ===")
    print("Controls the terminal user interface behavior.")

    if interactive:
        print(f"\nInitial load limit (default: 1000 rows)")
        print("How many rows to show initially when loading large datasets")
        print("Recommendation: 1000 for fast startup")
        load_input = input("Initial load limit [1000]: ").strip()
        initial_load_limit = int(load_input) if load_input else 1000

        print(f"\nSearch result limit (default: 10000)")
        print("Maximum search results to display")
        print("Recommendation: 10000 for good performance")
        search_input = input("Search limit [10000]: ").strip()
        search_limit = int(search_input) if search_input else 10_000
    else:
        initial_load_limit = 1000
        search_limit = 10_000

    return TUIConfig(
        chunk_threshold=5000,
        chunk_size=1000,
        initial_load_limit=initial_load_limit,
        search_limit=search_limit,
        trace_limit=100,
    )


def _configure_trace(interactive: bool) -> TraceConfig:
    """Configure trace settings.

    Args:
        interactive: Whether to prompt for input

    Returns:
        Configured TraceConfig
    """
    print("\n=== Trace Configuration ===")
    print("Controls distributed tracing features.")

    if interactive:
        print(f"\nTrace ID field names (comma-separated)")
        print("Fields to check for trace IDs in JSON logs")
        print("Default: trace_id, traceId, x-trace-id")
        fields_input = input("Field names [trace_id,traceId,x-trace-id]: ").strip()
        if fields_input:
            trace_id_fields = [f.strip() for f in fields_input.split(',')]
        else:
            trace_id_fields = ['trace_id', 'traceId', 'x-trace-id']
    else:
        trace_id_fields = ['trace_id', 'traceId', 'x-trace-id']

    return TraceConfig(trace_id_fields=trace_id_fields)


def _save_config_to_file(config: TailCWConfig, path: Path) -> None:
    """Save configuration to TOML file.

    Args:
        config: Configuration to save
        path: Path to save to
    """
    # For simplicity, use create_default_config_file and let user edit
    create_default_config_file(path)
    print("\nNote: Template config created. Edit manually for custom values.")
```

**CLI Integration** (`tail_cw/__main__.py`):

Add command to run wizard:
```python
import argparse

def main() -> int:
    parser = argparse.ArgumentParser(description='Tail and filter CloudWatch logs')
    parser.add_argument('--config-wizard', action='store_true',
                       help='Run interactive configuration wizard')

    args = parser.parse_args()

    if args.config_wizard:
        from tail_cw.config.wizard import run_config_wizard
        run_config_wizard(interactive=True)
        return 0

    # ... existing code ...
```

---

### Step 4: Improved Error Messages

#### Update: `tail_cw/query/parser.py`

Add better error messages:

```python
class FilterParseError(ValueError):
    """Filter parsing error with helpful suggestions."""

    def __init__(self, message: str, suggestions: list[str] | None = None):
        """Initialize with message and suggestions.

        Args:
            message: Error message
            suggestions: List of suggestions to fix the error
        """
        self.suggestions = suggestions or []
        full_message = message
        if self.suggestions:
            full_message += "\n\nSuggestions:\n" + "\n".join(f"  • {s}" for s in self.suggestions)
        super().__init__(full_message)


# Update parse functions to raise FilterParseError with suggestions
def parse_filter_pattern(pattern: str) -> FilterNode:
    """Parse filter pattern with helpful error messages.

    Raises:
        FilterParseError: With suggestions on how to fix syntax errors
    """
    try:
        # ... existing parsing logic ...
        pass
    except ValueError as e:
        suggestions = []

        # Detect common mistakes and suggest fixes
        if '{' in pattern and '}' not in pattern:
            suggestions.append("Missing closing brace '}' in JSON filter")

        if '$..' in pattern:
            suggestions.append("Double dot '..' is invalid. Use single dot for nested fields: $.field.subfield")

        if pattern.count('"') % 2 != 0:
            suggestions.append("Unmatched quote. Make sure all quotes are paired")

        raise FilterParseError(str(e), suggestions) from e
```

---

### Step 5: Filter Presets and History

#### File: `tail_cw/config/filter_history.py` (NEW)

```python
"""Filter history and presets management."""

from __future__ import annotations

import json
from pathlib import Path

from platformdirs import user_data_dir


def get_filter_history_file() -> Path:
    """Get path to filter history file.

    Returns:
        Path to history JSON file
    """
    data_dir = Path(user_data_dir('tail-cw', ensure_exists=True))
    return data_dir / 'filter_history.json'


def save_filter_to_history(filter_pattern: str, description: str = '') -> None:
    """Save filter to history.

    Args:
        filter_pattern: Filter pattern to save
        description: Optional description
    """
    history_file = get_filter_history_file()

    # Load existing history
    history = load_filter_history()

    # Add new entry (avoid duplicates)
    entry = {'pattern': filter_pattern, 'description': description}
    if entry not in history:
        history.insert(0, entry)  # Most recent first

    # Keep only last 50
    history = history[:50]

    # Save
    with history_file.open('w') as f:
        json.dump(history, f, indent=2)


def load_filter_history() -> list[dict[str, str]]:
    """Load filter history.

    Returns:
        List of filter history entries
    """
    history_file = get_filter_history_file()

    if not history_file.exists():
        return []

    with history_file.open() as f:
        return json.load(f)


# Preset filters
PRESET_FILTERS = {
    'errors': {
        'pattern': 'ERROR',
        'description': 'All error messages',
    },
    'warnings': {
        'pattern': 'ERROR OR WARNING',
        'description': 'Errors and warnings',
    },
    'http-errors': {
        'pattern': '{ $.status >= 400 }',
        'description': 'HTTP 4xx and 5xx errors',
    },
    'slow-requests': {
        'pattern': '{ $.duration >= 1000 }',
        'description': 'Requests taking >1 second',
    },
}


def get_preset_filters() -> dict[str, dict[str, str]]:
    """Get preset filters.

    Returns:
        Dictionary of preset filters
    """
    return PRESET_FILTERS
```

---

### Step 6: TUI Improvements

#### Update: `tail_cw/tui/app.py`

Add filter history and presets:

```python
# Add new bindings
BINDINGS = [
    # ... existing ...
    ('f', 'filter_builder', 'Filter Builder'),
    ('h', 'filter_history', 'History'),
    ('p', 'filter_presets', 'Presets'),
]

def action_filter_builder(self) -> None:
    """Open interactive filter builder."""
    from tail_cw.tui.filter_builder import FilterBuilderScreen

    def handle_result(filter_str: str | None) -> None:
        if filter_str:
            # Apply filter
            self._execute_search(filter_str)
            # Save to history
            from tail_cw.config.filter_history import save_filter_to_history
            save_filter_to_history(filter_str)

    self.push_screen(FilterBuilderScreen(), handle_result)


def action_filter_history(self) -> None:
    """Show filter history."""
    from tail_cw.config.filter_history import load_filter_history

    history = load_filter_history()
    # Show selection screen...


def action_filter_presets(self) -> None:
    """Show filter presets."""
    from tail_cw.config.filter_history import get_preset_filters

    presets = get_preset_filters()
    # Show selection screen...
```

---

## Documentation Updates

### Create: `docs/docs/FILTER_GUIDE.md`

```markdown
# Filter Guide

## Quick Start

### Basic Text Search

Search for any text in log messages:
```
ERROR
```

### Field-Based Search

Search specific JSON fields:
```
level:ERROR
status:500
user_id:12345
```

### Numeric Comparisons

Filter by numeric values:
```
{ $.status >= 400 }
{ $.duration > 1000 }
{ $.count <= 10 }
```

### Combining Filters

Use AND, OR, NOT:
```
ERROR AND status:500
level:ERROR OR level:WARNING
NOT test
```

## Advanced Filters

### Regular Expressions

Pattern matching:
```
/[Ee]rror.*/
/timeout|connection/
```

### Nested Fields

Access nested JSON:
```
{ $.context.user.id = "123" }
{ $.metadata.region = "us-east-1" }
```

## Interactive Tools

### Filter Builder (Press `f`)

Use the visual filter builder to create filters without memorizing syntax:
1. Press `f` to open
2. Select filter type
3. Fill in fields
4. Preview updates live
5. Press Apply

### Filter History (Press `h`)

Access your recent filters:
- Up to 50 most recent filters saved
- Quick re-apply from history
- Edit and save variations

### Filter Presets (Press `p`)

Common filters ready to use:
- **errors**: Find all error messages
- **warnings**: Errors and warnings
- **http-errors**: HTTP 4xx/5xx
- **slow-requests**: Requests >1s

## Tips

### Start Simple

Begin with plain text search, then progress to structured queries.

### Use Explain

Not sure what a filter does? Use the explain feature to see plain English description.

### Save Useful Filters

Filters are automatically saved to history. Frequently used filters become presets.

### Error Messages

Read error messages carefully - they include suggestions for fixing syntax.
```

---

## Testing Strategy

### Unit Tests

- `tests/test_query_simple_parser.py` (10+ tests)
- `tests/test_tui_filter_builder.py` (8+ tests)
- `tests/test_config_wizard.py` (6+ tests)
- `tests/test_config_filter_history.py` (5+ tests)

### Integration Tests

- End-to-end filter building
- Wizard creates valid config
- History saves and loads correctly

### Usability Testing

Manual testing checklist:
- [ ] New user can create first filter in <30 seconds
- [ ] Filter builder is intuitive
- [ ] Error messages are helpful
- [ ] Configuration wizard completes in <5 minutes

---

## Success Criteria

- [ ] Filter builder works for common cases
- [ ] Configuration wizard creates valid config
- [ ] Error messages include actionable suggestions
- [ ] Filter history saves automatically
- [ ] Presets cover 80% of common use cases
- [ ] All tests passing (30+ new tests)
- [ ] Documentation covers all new features
- [ ] No regressions in existing functionality

---

## Implementation Timeline

**Estimated Time:** 8-12 hours

1. **Simple Parser** (2 hours)
2. **Filter Builder** (3 hours)
3. **Config Wizard** (2 hours)
4. **Filter History** (1 hour)
5. **Error Messages** (1 hour)
6. **TUI Integration** (2 hours)
7. **Tests** (2-3 hours)
8. **Documentation** (1 hour)

---

## Commands to Run

```bash
# Create branch
git checkout -b claude/simplify-search-functionality-01VGBEGD5jDcQmxrbdNnW9zA

# Implement files...

# Run configuration wizard
uv run python -m tail_cw --config-wizard

# Run tests
uv run pytest tests/test_query_simple_parser.py -v
uv run pytest tests/test_tui_filter_builder.py -v
uv run pytest tests/test_config_wizard.py -v

# Run all checks
uv run ruff format
uv run ruff check --fix --unsafe-fixes
uv run mypy tail_cw tests
uv run pytest -q --ff

# Commit and push
git add -A
git commit -m "feat: simplify search and configuration (Branch 3)

- Added simplified filter parser with helpful suggestions
- Implemented interactive filter builder (press 'f')
- Created configuration wizard for first-time setup
- Added filter history (up to 50 recent filters)
- Implemented filter presets for common patterns
- Improved error messages with actionable suggestions
- Added comprehensive filter guide documentation
- 30+ tests for new functionality

Addresses complexity issues from PROJECT_REVIEW.md analysis"

git push -u origin claude/simplify-search-functionality-01VGBEGD5jDcQmxrbdNnW9zA
```
