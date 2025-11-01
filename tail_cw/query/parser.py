"""CloudWatch-style filter pattern parser.

This module provides a parser for CloudWatch Logs filter patterns, supporting:
- Plain text search: ERROR
- Quoted phrase search: "connection timeout"
- Regex patterns: %[Ee]rror%
- JSON field filters: { $.level = "ERROR" }
- Numeric comparisons: { $.status >= 500 }
- Field existence checks: { $.userId = * }
- Extended key:value syntax: level:ERROR

The parser builds an AST (abstract syntax tree) of FilterNode objects that can be
translated to backend-specific queries (DuckDB SQL or Polars expressions).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum


class FilterNodeType(Enum):
    """Type of filter node in the AST."""

    TEXT_SEARCH = 'text_search'  # Plain text search (e.g., 'ERROR')
    EXACT_PHRASE = 'exact_phrase'  # Quoted phrase search (e.g., '"connection timeout"')
    REGEX = 'regex'  # Regex pattern (e.g., '%[Ee]rror%')
    JSON_FIELD_EQUALS = 'json_field_equals'  # JSON field equality (e.g., '{ $.level = "ERROR" }')
    JSON_FIELD_NOT_EQUALS = 'json_field_not_equals'  # JSON field inequality
    JSON_FIELD_EXISTS = 'json_field_exists'  # JSON field existence (e.g., '{ $.userId = * }')
    JSON_FIELD_NUMERIC = 'json_field_numeric'  # Numeric comparison (e.g., '{ $.status >= 500 }')
    JSON_FIELD_REGEX = 'json_field_regex'  # JSON field regex match (e.g., '{ $.level = %[Ee]rror% }')
    AND = 'and'  # Logical AND of multiple conditions
    OR = 'or'  # Logical OR of multiple conditions
    NOT = 'not'  # Logical NOT
    MATCH_ALL = 'match_all'  # Match all records (no filter)


@dataclass(frozen=True)
class FilterNode:
    """AST node representing a filter condition.

    Attributes:
        node_type: Type of filter node (TEXT_SEARCH, JSON_FIELD_EQUALS, AND, etc.)
        value: Value for text/field searches (e.g., 'ERROR', '500')
        field_path: JSON field path components (e.g., ['level'], ['context', 'user', 'id'])
        operator: Comparison operator for numeric/string comparisons (=, !=, >, <, >=, <=)
        children: Child nodes for AND/OR/NOT operations

    Examples:
        Text search:
            FilterNode(node_type=TEXT_SEARCH, value='ERROR')

        JSON field equality:
            FilterNode(node_type=JSON_FIELD_EQUALS, field_path=['level'], value='ERROR')

        Numeric comparison:
            FilterNode(node_type=JSON_FIELD_NUMERIC, field_path=['status'],
                      operator='>=', value='500')

        Logical AND:
            FilterNode(node_type=AND, children=[node1, node2])
    """

    node_type: FilterNodeType
    value: str | None = None
    field_path: list[str] | None = None
    operator: str | None = None
    children: list[FilterNode] | None = None


def parse_filter_pattern(pattern: str) -> FilterNode:
    """Parse a CloudWatch-style filter pattern into an AST.

    Supports the following pattern types:
    - Plain text: ERROR
    - Quoted phrase: "connection timeout"
    - Regex: %[Ee]rror%
    - JSON field filters: { $.level = "ERROR" }
    - Multiple JSON conditions: { $.level = "ERROR" $.status >= 500 }
    - Numeric comparisons: { $.status >= 500 }
    - Field existence: { $.userId = * }

    Args:
        pattern: CloudWatch filter pattern string

    Returns:
        Root FilterNode of the parsed AST

    Raises:
        ValueError: If the pattern is malformed or contains invalid syntax

    Examples:
        >>> parse_filter_pattern('ERROR')
        FilterNode(node_type=TEXT_SEARCH, value='ERROR')

        >>> parse_filter_pattern('{ $.level = "ERROR" }')
        FilterNode(node_type=JSON_FIELD_EQUALS, field_path=['level'], value='ERROR')

        >>> parse_filter_pattern('%[Ee]rror%')
        FilterNode(node_type=REGEX, value='[Ee]rror')
    """
    pattern = pattern.strip()

    if pattern.startswith('{') != pattern.endswith('}'):
        msg = 'Mismatched braces in filter pattern'
        raise ValueError(msg)

    # Empty pattern matches all
    if not pattern:
        return FilterNode(node_type=FilterNodeType.MATCH_ALL)

    # Detect pattern type
    if pattern.startswith('{') and pattern.endswith('}'):
        return _parse_json_filter(pattern)
    if pattern.startswith('%') and pattern.endswith('%'):
        return _parse_regex_filter(pattern)
    return _parse_text_filter(pattern)


def _parse_json_filter(pattern: str) -> FilterNode:
    """Parse JSON field filter pattern.

    Supports:
    - Field equality: { $.level = "ERROR" }
    - Field inequality: { $.level != "DEBUG" }
    - Numeric comparison: { $.status >= 500 }
    - Field existence: { $.userId = * }
    - Multiple conditions (implicit AND): { $.level = "ERROR" $.status >= 500 }
    - Nested fields: { $.context.user.id = "123" }

    Args:
        pattern: Pattern starting with '{' and ending with '}'

    Returns:
        FilterNode representing the JSON filter(s)

    Raises:
        ValueError: If the pattern is malformed

    Examples:
        >>> _parse_json_filter('{ $.level = "ERROR" }')
        FilterNode(node_type=JSON_FIELD_EQUALS, field_path=['level'], value='ERROR')

        >>> _parse_json_filter('{ $.status >= 500 }')
        FilterNode(node_type=JSON_FIELD_NUMERIC, field_path=['status'],
                   operator='>=', value='500')
    """
    # Extract content between braces
    content = pattern[1:-1].strip()
    if not content:
        msg = 'Empty JSON filter pattern'
        raise ValueError(msg)

    # Split into individual conditions, keeping quoted strings intact.
    raw_conditions = [part.strip() for part in re.split(r'(?=\$\.)', content) if part.strip()]
    conditions: list[str] = []

    for part in raw_conditions:
        if not part.startswith('$.'):
            msg = f'Invalid JSON condition: {part}'
            raise ValueError(msg)
        conditions.append(part)

    if not conditions:
        msg = 'No valid conditions found in JSON filter'
        raise ValueError(msg)

    # Parse each condition
    nodes = [_parse_json_condition(cond) for cond in conditions]

    # Combine with AND if multiple conditions
    if len(nodes) == 1:
        return nodes[0]
    return FilterNode(node_type=FilterNodeType.AND, children=nodes)


def _parse_json_condition(condition: str) -> FilterNode:
    """Parse a single JSON field condition.

    Args:
        condition: Single condition string (e.g., '$.level = "ERROR"')

    Returns:
        FilterNode for the condition

    Raises:
        ValueError: If the condition is malformed
    """
    # Match pattern: $.field.path operator value
    # Operators: =, !=, >, <, >=, <=
    operator_pattern = r'\$\.([a-zA-Z0-9_.]+)\s*(>=|<=|!=|=|>|<)\s*(.+)'
    match = re.match(operator_pattern, condition)

    if not match:
        msg = f'Invalid JSON condition: {condition}'
        raise ValueError(msg)

    field_path_str, operator, value = match.groups()
    field_path = _normalize_field_path(field_path_str)
    value = value.strip()

    # Handle special value '*' for existence checks
    if value == '*':
        return FilterNode(
            node_type=FilterNodeType.JSON_FIELD_EXISTS,
            field_path=field_path,
        )

    # Remove quotes from string values
    if value.startswith('"') and value.endswith('"'):
        value = value[1:-1]

    # Handle regex values (%...%)
    # Minimum length for valid regex pattern is 3 (e.g., '%x%')
    min_regex_length = 3
    if value.startswith('%') and value.endswith('%') and len(value) > min_regex_length - 1:
        regex_pattern = value[1:-1]
        # Validate regex syntax
        try:
            re.compile(regex_pattern)
        except re.error as e:
            msg = f'Invalid regex pattern in value: {e}'
            raise ValueError(msg) from e

        return FilterNode(
            node_type=FilterNodeType.JSON_FIELD_REGEX,
            field_path=field_path,
            value=regex_pattern,
        )

    # Determine node type based on operator
    if operator == '=':
        # Check if value is numeric
        try:
            float(value)
            return FilterNode(
                node_type=FilterNodeType.JSON_FIELD_NUMERIC,
                field_path=field_path,
                operator='=',
                value=value,
            )
        except ValueError:
            return FilterNode(
                node_type=FilterNodeType.JSON_FIELD_EQUALS,
                field_path=field_path,
                value=value,
            )
    elif operator == '!=':
        return FilterNode(
            node_type=FilterNodeType.JSON_FIELD_NOT_EQUALS,
            field_path=field_path,
            value=value,
        )
    else:  # >, <, >=, <=
        return FilterNode(
            node_type=FilterNodeType.JSON_FIELD_NUMERIC,
            field_path=field_path,
            operator=operator,
            value=value,
        )


def _normalize_field_path(field_path: str) -> list[str]:
    """Normalize JSON field path to list of components.

    Args:
        field_path: Field path like 'level' or 'context.user.id'

    Returns:
        List of field components ['level'] or ['context', 'user', 'id']

    Raises:
        ValueError: If field path contains invalid characters

    Examples:
        >>> _normalize_field_path('level')
        ['level']

        >>> _normalize_field_path('context.user.id')
        ['context', 'user', 'id']
    """
    # Split by '.' to get field components
    components = field_path.split('.')

    # Validate field names (alphanumeric, underscore, hyphen)
    valid_pattern = re.compile(r'^[a-zA-Z0-9_-]+$')
    for component in components:
        if not valid_pattern.match(component):
            msg = f'Invalid field name: {component}'
            raise ValueError(msg)

    return components


def _parse_regex_filter(pattern: str) -> FilterNode:
    """Parse regex pattern filter.

    Args:
        pattern: Pattern with % delimiters (e.g., '%[Ee]rror%')

    Returns:
        FilterNode with REGEX type

    Raises:
        ValueError: If the regex is invalid

    Examples:
        >>> _parse_regex_filter('%[Ee]rror%')
        FilterNode(node_type=REGEX, value='[Ee]rror')
    """
    # Extract regex between % delimiters
    if not pattern.startswith('%') or not pattern.endswith('%'):
        msg = 'Regex pattern must be enclosed in % delimiters'
        raise ValueError(msg)

    regex_pattern = pattern[1:-1]

    # Validate regex syntax
    try:
        re.compile(regex_pattern)
    except re.error as e:
        msg = f'Invalid regex pattern: {e}'
        raise ValueError(msg) from e

    return FilterNode(node_type=FilterNodeType.REGEX, value=regex_pattern)


def _parse_text_filter(pattern: str) -> FilterNode:
    """Parse plain text or quoted phrase filter.

    Args:
        pattern: Plain text or quoted phrase

    Returns:
        FilterNode with TEXT_SEARCH or EXACT_PHRASE type

    Examples:
        >>> _parse_text_filter('ERROR')
        FilterNode(node_type=TEXT_SEARCH, value='ERROR')

        >>> _parse_text_filter('"connection timeout"')
        FilterNode(node_type=EXACT_PHRASE, value='connection timeout')
    """
    # Check if pattern is quoted
    if pattern.startswith('"') and pattern.endswith('"'):
        # Exact phrase search
        value = pattern[1:-1]
        return FilterNode(node_type=FilterNodeType.EXACT_PHRASE, value=value)

    # Handle multiple space-separated terms as implicit AND
    terms = pattern.split()
    if len(terms) > 1:
        nodes = [FilterNode(node_type=FilterNodeType.TEXT_SEARCH, value=term) for term in terms]
        return FilterNode(node_type=FilterNodeType.AND, children=nodes)

    # Single term text search
    return FilterNode(node_type=FilterNodeType.TEXT_SEARCH, value=pattern)


def parse_extended_filter(pattern: str) -> FilterNode:
    """Parse extended key:value filter syntax.

    This extends CloudWatch syntax for local querying of parsed JSONL fields.

    Supports:
    - Field equality: level:ERROR
    - Numeric comparison: status:>=500
    - Field existence: user.id:*
    - Nested fields: user.id:123

    Args:
        pattern: Extended filter like 'level:ERROR' or 'status:>=500'

    Returns:
        FilterNode for the extended filter

    Raises:
        ValueError: If the pattern is malformed

    Examples:
        >>> parse_extended_filter('level:ERROR')
        FilterNode(node_type=JSON_FIELD_EQUALS, field_path=['level'], value='ERROR')

        >>> parse_extended_filter('status:>=500')
        FilterNode(node_type=JSON_FIELD_NUMERIC, field_path=['status'],
                   operator='>=', value='500')
    """
    # Split by ':' to get field and value
    if ':' not in pattern:
        msg = f'Extended filter must contain ":" separator: {pattern}'
        raise ValueError(msg)

    field, value = pattern.split(':', 1)
    field = field.strip()
    value = value.strip()

    if not field or not value:
        msg = f'Extended filter has empty field or value: {pattern}'
        raise ValueError(msg)

    # Normalize field path
    field_path = _normalize_field_path(field)

    # Handle special value '*' for existence checks
    if value == '*':
        return FilterNode(
            node_type=FilterNodeType.JSON_FIELD_EXISTS,
            field_path=field_path,
        )

    # Detect operator in value
    operator_pattern = r'^(>=|<=|!=|=|>|<)(.+)$'
    match = re.match(operator_pattern, value)

    if match:
        operator, val = match.groups()
        val = val.strip()

        # Strip surrounding quotes if present
        if val.startswith('"') and val.endswith('"'):
            val = val[1:-1]

        # Determine if numeric comparison
        try:
            float(val)
            return FilterNode(
                node_type=FilterNodeType.JSON_FIELD_NUMERIC,
                field_path=field_path,
                operator=operator,
                value=val,
            )
        except ValueError:
            # String comparison
            if operator in {'>', '<', '>=', '<='}:
                msg = f'Numeric operators require numeric values: {pattern}'
                raise ValueError(msg) from None
            if operator == '=':
                return FilterNode(
                    node_type=FilterNodeType.JSON_FIELD_EQUALS,
                    field_path=field_path,
                    value=val,
                )
            return FilterNode(
                node_type=FilterNodeType.JSON_FIELD_NOT_EQUALS,
                field_path=field_path,
                value=val,
            )

    # No operator, assume equality
    # Strip surrounding quotes if present
    if value.startswith('"') and value.endswith('"'):
        value = value[1:-1]

    return FilterNode(
        node_type=FilterNodeType.JSON_FIELD_EQUALS,
        field_path=field_path,
        value=value,
    )


def combine_filters(
    nodes: Sequence[FilterNode],
    operator: str = 'AND',
) -> FilterNode:
    """Combine multiple filter nodes with AND/OR operator.

    Args:
        nodes: List of filter nodes to combine
        operator: Logical operator ('AND' or 'OR')

    Returns:
        Combined FilterNode

    Raises:
        ValueError: If operator is invalid or nodes is empty

    Examples:
        >>> node1 = FilterNode(node_type=TEXT_SEARCH, value='ERROR')
        >>> node2 = FilterNode(node_type=TEXT_SEARCH, value='WARN')
        >>> combine_filters([node1, node2], 'OR')
        FilterNode(node_type=OR, children=[node1, node2])
    """
    if not nodes:
        msg = 'Cannot combine empty list of nodes'
        raise ValueError(msg)

    if len(nodes) == 1:
        return nodes[0]

    # Validate operator
    if operator.upper() not in {'AND', 'OR'}:
        msg = f'Invalid operator: {operator}. Must be AND or OR'
        raise ValueError(msg)

    node_type = FilterNodeType.AND if operator.upper() == 'AND' else FilterNodeType.OR

    # Flatten nested nodes of same type for optimization
    flattened_children = []
    for node in nodes:
        if node.node_type == node_type and node.children:
            flattened_children.extend(node.children)
        else:
            flattened_children.append(node)

    return FilterNode(node_type=node_type, children=flattened_children)


def filter_to_string(node: FilterNode) -> str:
    """Convert FilterNode back to human-readable string.

    Returns:
        Human-readable representation of the filter tree.
    """
    builder = _FILTER_TO_STRING_BUILDERS.get(node.node_type)

    return f'UNKNOWN: {node.node_type}' if builder is None else builder(node)


def _string_match_all(_node: FilterNode) -> str:
    return 'MATCH_ALL'


def _string_text_search(node: FilterNode) -> str:
    return f'TEXT_SEARCH: {node.value}'


def _string_exact_phrase(node: FilterNode) -> str:
    return f'EXACT_PHRASE: "{node.value}"'


def _string_regex(node: FilterNode) -> str:
    return f'REGEX: %{node.value}%'


def _string_json_equals(node: FilterNode) -> str:
    field = '.'.join(node.field_path or [])
    return f'JSON_FIELD: $.{field} = {node.value}'


def _string_json_not_equals(node: FilterNode) -> str:
    field = '.'.join(node.field_path or [])
    return f'JSON_FIELD: $.{field} != {node.value}'


def _string_json_exists(node: FilterNode) -> str:
    field = '.'.join(node.field_path or [])
    return f'JSON_FIELD_EXISTS: $.{field}'


def _string_json_numeric(node: FilterNode) -> str:
    field = '.'.join(node.field_path or [])
    return f'JSON_FIELD_NUMERIC: $.{field} {node.operator} {node.value}'


def _string_json_regex(node: FilterNode) -> str:
    field = '.'.join(node.field_path or [])
    return f'JSON_FIELD_REGEX: $.{field} ~ %{node.value}%'


def _string_and(node: FilterNode) -> str:
    children = node.children or []
    child_strings = [filter_to_string(child) for child in children]
    joined = ' AND '.join(child_strings)
    return f'({joined})'


def _string_or(node: FilterNode) -> str:
    children = node.children or []
    child_strings = [filter_to_string(child) for child in children]
    joined = ' OR '.join(child_strings)
    return f'({joined})'


def _string_not(node: FilterNode) -> str:
    child_str = filter_to_string(node.children[0]) if node.children else ''
    return f'NOT ({child_str})'


FilterStringBuilder = Callable[[FilterNode], str]
_FILTER_TO_STRING_BUILDERS: dict[FilterNodeType, FilterStringBuilder] = {
    FilterNodeType.MATCH_ALL: _string_match_all,
    FilterNodeType.TEXT_SEARCH: _string_text_search,
    FilterNodeType.EXACT_PHRASE: _string_exact_phrase,
    FilterNodeType.REGEX: _string_regex,
    FilterNodeType.JSON_FIELD_EQUALS: _string_json_equals,
    FilterNodeType.JSON_FIELD_NOT_EQUALS: _string_json_not_equals,
    FilterNodeType.JSON_FIELD_EXISTS: _string_json_exists,
    FilterNodeType.JSON_FIELD_NUMERIC: _string_json_numeric,
    FilterNodeType.JSON_FIELD_REGEX: _string_json_regex,
    FilterNodeType.AND: _string_and,
    FilterNodeType.OR: _string_or,
    FilterNodeType.NOT: _string_not,
}
