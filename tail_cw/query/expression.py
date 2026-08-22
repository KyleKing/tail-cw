"""Boolean expressions over filter terms: ``AND``, ``OR``, ``NOT``, and parentheses.

The AST in :mod:`tail_cw.query.parser` has held ``AND``, ``OR``, and ``NOT`` from the
start and no surface syntax reached them, so ``ERROR OR WARNING`` parsed as three text
terms including the literal word ``OR``. This module is that surface.

It is deliberately local-only. CloudWatch's own pattern syntax can express some of what
this accepts (``?a ?b`` is an OR of text terms, ``a -b`` excludes) but not a mixed
expression, and its documented behaviour for the mixed case is to **silently ignore** the
``?`` terms rather than reject the pattern. A wrong answer that looks right is worse than
no answer, so :func:`portable_filter_pattern` translates only what CloudWatch can mean
exactly and refuses the rest by name.

Keywords are uppercase. A log line saying "timed out or retried" is a text search, and
making ``or`` an operator would quietly change what that search means.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from tail_cw.query.parser import (
    FilterNode,
    FilterNodeType,
    parse_extended_filter,
    parse_filter_pattern,
)

AND = 'AND'
OR = 'OR'
NOT = 'NOT'
_KEYWORDS = frozenset({AND, OR, NOT})
_OPEN = '('
_CLOSE = ')'


class FilterParseError(ValueError):
    """A filter that could not be parsed, with the fix where one is knowable.

    Separate from the bare ``ValueError`` the term parsers raise, because the terse text
    of those ("Mismatched braces in filter pattern") reached the user with no indication
    of what to type instead.
    """

    def __init__(self, message: str, *, suggestion: str = '') -> None:
        """Report ``message``, appending ``suggestion`` when there is one to give."""
        self.suggestion = suggestion
        super().__init__(f'{message}. {suggestion}' if suggestion else message)


@dataclass
class _Cursor:
    """Position in the token list, so the recursive descent stays readable."""

    tokens: list[str]
    index: int = 0

    def peek(self) -> str | None:
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def take(self) -> str:
        token = self.tokens[self.index]
        self.index += 1
        return token


_TOKEN_RE = re.compile(
    r"""
      "[^"]*"        # a quoted phrase, whitespace and all
    | \{[^{}]*\}     # a JSON filter, which CloudWatch never nests
    | %[^%]*%        # a regex, delimited by percent signs
    | [()]           # grouping
    | [^\s()]+       # a bare term
    """,
    re.VERBOSE,
)


def _tokenize(text: str) -> list[str]:
    """Split into terms, keywords, and parentheses, keeping quotes and braces whole.

    A quoted phrase, a ``{...}`` JSON filter, and a ``%regex%`` are single tokens
    whatever whitespace they contain, which is what makes
    ``"connection timeout" OR { $.status >= 500 }`` parse.
    """
    if text.count('"') % 2:
        msg = 'Unbalanced quote in filter'
        raise FilterParseError(msg, suggestion='Close the phrase with a second "')
    if text.count('{') != text.count('}'):
        msg = 'Mismatched braces in filter'
        raise FilterParseError(msg, suggestion='A JSON filter looks like { $.level = "ERROR" }')
    return _TOKEN_RE.findall(text)


def parse_query(text: str) -> FilterNode:
    """Parse a full filter expression into an AST.

    Args:
        text: A filter, optionally combining terms with ``AND``, ``OR``, ``NOT``, and
            parentheses. Bare whitespace between terms still means ``AND``, which is
            what CloudWatch's own syntax means by it.

    Returns:
        The root node. An empty filter is :attr:`FilterNodeType.MATCH_ALL`.

    Raises:
        FilterParseError: If the expression is malformed.

    Examples:
        >>> parse_query('ERROR OR WARNING')
        FilterNode(node_type=OR, children=[...])

        >>> parse_query('level:error AND NOT status:200')
        FilterNode(node_type=AND, children=[...])
    """
    tokens = _tokenize(text)
    if not tokens:
        return FilterNode(node_type=FilterNodeType.MATCH_ALL)
    cursor = _Cursor(tokens)
    node = _parse_or(cursor)
    if (extra := cursor.peek()) is not None:
        msg = f'Unexpected {extra!r} in filter'
        suggestion = 'Remove the stray )' if extra == _CLOSE else 'Terms are joined by AND, OR, or a space'
        raise FilterParseError(msg, suggestion=suggestion)
    return node


def _parse_or(cursor: _Cursor) -> FilterNode:
    children = [_parse_and(cursor)]
    while cursor.peek() == OR:
        cursor.take()
        children.append(_parse_and(cursor))
    return children[0] if len(children) == 1 else FilterNode(node_type=FilterNodeType.OR, children=children)


def _parse_and(cursor: _Cursor) -> FilterNode:
    children = [_parse_unary(cursor)]
    while (token := cursor.peek()) is not None and token not in {OR, _CLOSE}:
        if token == AND:
            cursor.take()
        children.append(_parse_unary(cursor))
    return children[0] if len(children) == 1 else FilterNode(node_type=FilterNodeType.AND, children=children)


def _parse_unary(cursor: _Cursor) -> FilterNode:
    token = cursor.peek()
    if token is None:
        msg = 'Filter ends after an operator'
        raise FilterParseError(msg, suggestion='Every AND, OR, and NOT needs a term after it')
    if token == NOT:
        cursor.take()
        return FilterNode(node_type=FilterNodeType.NOT, children=[_parse_unary(cursor)])
    if token == _OPEN:
        cursor.take()
        node = _parse_or(cursor)
        if cursor.peek() != _CLOSE:
            msg = 'Unclosed ( in filter'
            raise FilterParseError(msg, suggestion='Add the matching )')
        cursor.take()
        return node
    if token in {AND, OR, _CLOSE}:
        msg = f'Filter starts with {token!r}'
        raise FilterParseError(msg, suggestion='Put a term before the operator')
    return _parse_term(cursor.take())


def _parse_term(token: str) -> FilterNode:
    """Parse one term with the existing parsers, adding the fix to their terse errors."""
    try:
        if _is_field_term(token):
            return parse_extended_filter(token)
        return parse_filter_pattern(token)
    except FilterParseError:
        raise
    except ValueError as err:
        raise FilterParseError(str(err), suggestion=_suggest(token)) from err


def _is_field_term(token: str) -> bool:
    """Whether a token reads as ``field:value`` rather than as text to search for."""
    if token.startswith(('{', '"', '%')) or ':' not in token:
        return False
    field, _, value = token.partition(':')
    return bool(field) and bool(value) and not field.startswith('$')


def _suggest(token: str) -> str:
    """The likeliest fix for a term the parsers rejected."""
    if '$..' in token:
        return 'A field path uses one dot per level, as in $.context.user.id'
    if token.count('"') % 2:
        return 'Close the phrase with a second "'
    if token.startswith('{') or token.endswith('}'):
        return 'A JSON filter looks like { $.level = "ERROR" }'
    return 'Use field:value for a record field, or %re% for a regex'


@dataclass(frozen=True)
class Portability:
    """Whether a filter can be sent to CloudWatch, and the pattern if it can.

    Attributes:
        pattern: The CloudWatch filter pattern, or None when the filter is local-only.
        reason: Why it cannot be sent, empty when it can.
    """

    pattern: str | None
    reason: str = ''


_TEXT_TYPES = frozenset({FilterNodeType.TEXT_SEARCH, FilterNodeType.EXACT_PHRASE})


def portable_filter_pattern(node: FilterNode) -> Portability:
    """Translate a filter for CloudWatch, or say why it cannot be translated.

    The refusals are not conservatism. CloudWatch documents that combining its ``?``
    (any-of) terms with anything else makes it **ignore the ``?`` terms**, so a mixed
    expression sent as a pattern comes back with the wrong events and no error. The only
    safe translations are the ones CloudWatch can mean exactly:

    - one text term, phrase, or regex
    - an AND of text terms, which is what a space already means
    - an OR of text terms, as ``?a ?b``
    - an AND of text terms with exclusions, as ``a -b``
    - a JSON-only tree, which has real ``&&``, ``||``, and parentheses
    """
    if node.node_type is FilterNodeType.MATCH_ALL:
        return Portability(pattern=None, reason='an empty filter matches everything')
    if _is_json_only(node):
        return Portability(pattern=_json_pattern(node))
    builder = _PORTABLE_BUILDERS.get(node.node_type)
    if builder is None:
        return Portability(pattern=None, reason=f'CloudWatch has no {node.node_type.value} over text')
    return builder(node)


def _portable_text(node: FilterNode) -> Portability:
    return Portability(pattern=str(node.value))


def _portable_phrase(node: FilterNode) -> Portability:
    return Portability(pattern=f'"{node.value}"')


def _portable_regex(node: FilterNode) -> Portability:
    return Portability(pattern=f'%{node.value}%')


def _portable_or(node: FilterNode) -> Portability:
    children = node.children or []
    if all(child.node_type in _TEXT_TYPES for child in children):
        return Portability(pattern=' '.join(f'?{_text_of(child)}' for child in children))
    return Portability(
        pattern=None,
        reason='CloudWatch can only OR plain text terms, and mixing its ?terms with anything else '
        'makes it ignore them instead of failing',
    )


def _portable_and(node: FilterNode) -> Portability:
    children = node.children or []
    includes: list[str] = []
    excludes: list[str] = []
    for child in children:
        if child.node_type in _TEXT_TYPES:
            includes.append(_text_of(child))
        elif child.node_type is FilterNodeType.NOT and child.children:
            inner = child.children[0]
            if inner.node_type not in _TEXT_TYPES:
                return Portability(pattern=None, reason='CloudWatch can only exclude plain text terms')
            excludes.append(f'-{_text_of(inner)}')
        else:
            return Portability(pattern=None, reason='CloudWatch cannot AND text terms with anything else')
    if not includes:
        return Portability(pattern=None, reason='CloudWatch has no pattern that only excludes')
    return Portability(pattern=' '.join([*includes, *excludes]))


def _text_of(node: FilterNode) -> str:
    return f'"{node.value}"' if node.node_type is FilterNodeType.EXACT_PHRASE else str(node.value)


def _is_json_only(node: FilterNode) -> bool:
    """Whether every leaf reads a record field, which is the case CloudWatch can nest."""
    if node.node_type in {FilterNodeType.AND, FilterNodeType.OR, FilterNodeType.NOT}:
        children = node.children or []
        return bool(children) and all(_is_json_only(child) for child in children)
    return node.node_type.value.startswith('json_field_')


def _json_pattern(node: FilterNode) -> str:
    return f'{{ {_json_condition(node)} }}'


def _json_condition(node: FilterNode) -> str:
    return _JSON_CONDITION_BUILDERS.get(node.node_type, _json_equals)(node)


def _json_all(node: FilterNode) -> str:
    return ' && '.join(f'({_json_condition(child)})' for child in node.children or [])


def _json_any(node: FilterNode) -> str:
    return ' || '.join(f'({_json_condition(child)})' for child in node.children or [])


def _json_negated(node: FilterNode) -> str:
    """CloudWatch has no NOT of a condition, only ``!=`` on one and ``NOT EXISTS``."""
    children = node.children or []
    if not children:
        return ''
    inner = children[0]
    if inner.node_type is FilterNodeType.JSON_FIELD_EQUALS:
        return f'{_selector(inner)} != {_json_value(inner)}'
    return f'{_selector(inner)} NOT EXISTS'


def _json_numeric(node: FilterNode) -> str:
    return f'{_selector(node)} {node.operator} {node.value}'


def _json_not_equals(node: FilterNode) -> str:
    return f'{_selector(node)} != {_json_value(node)}'


def _json_exists(node: FilterNode) -> str:
    return f'{_selector(node)} = *'


def _json_regex(node: FilterNode) -> str:
    return f'{_selector(node)} = %{node.value}%'


def _json_equals(node: FilterNode) -> str:
    return f'{_selector(node)} = {_json_value(node)}'


def _selector(node: FilterNode) -> str:
    return '$.' + '.'.join(node.field_path or [])


def _json_value(node: FilterNode) -> str:
    return f'"{node.value}"'


_PORTABLE_BUILDERS: dict[FilterNodeType, Callable[[FilterNode], Portability]] = {
    FilterNodeType.TEXT_SEARCH: _portable_text,
    FilterNodeType.EXACT_PHRASE: _portable_phrase,
    FilterNodeType.REGEX: _portable_regex,
    FilterNodeType.OR: _portable_or,
    FilterNodeType.AND: _portable_and,
}

_JSON_CONDITION_BUILDERS: dict[FilterNodeType, Callable[[FilterNode], str]] = {
    FilterNodeType.AND: _json_all,
    FilterNodeType.OR: _json_any,
    FilterNodeType.NOT: _json_negated,
    FilterNodeType.JSON_FIELD_NUMERIC: _json_numeric,
    FilterNodeType.JSON_FIELD_NOT_EQUALS: _json_not_equals,
    FilterNodeType.JSON_FIELD_EXISTS: _json_exists,
    FilterNodeType.JSON_FIELD_REGEX: _json_regex,
}
