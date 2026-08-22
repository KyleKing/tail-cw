"""Cover the boolean filter surface and what of it CloudWatch can be told."""

from __future__ import annotations

import pytest

from tail_cw.query.expression import (
    FilterParseError,
    parse_query,
    portable_filter_pattern,
)
from tail_cw.query.parser import FilterNodeType, filter_to_string


def _types(text: str) -> list[str]:
    node = parse_query(text)
    return [child.node_type.value for child in node.children or []]


def test_or_reaches_the_ast_it_never_used_to() -> None:
    """`ERROR OR WARNING` used to parse as three text terms including the word OR."""
    node = parse_query('ERROR OR WARNING')

    assert node.node_type is FilterNodeType.OR
    assert _types('ERROR OR WARNING') == ['text_search', 'text_search']
    assert [child.value for child in node.children or []] == ['ERROR', 'WARNING']


def test_a_space_still_means_and() -> None:
    """Which is what CloudWatch's own syntax means by it, so the default cannot change."""
    assert parse_query('ERROR ARGUMENTS').node_type is FilterNodeType.AND
    assert parse_query('ERROR AND ARGUMENTS').node_type is FilterNodeType.AND


def test_and_binds_tighter_than_or() -> None:
    node = parse_query('a AND b OR c')

    assert node.node_type is FilterNodeType.OR
    children = node.children or []
    assert children[0].node_type is FilterNodeType.AND
    assert children[1].node_type is FilterNodeType.TEXT_SEARCH


def test_parentheses_override_the_precedence() -> None:
    node = parse_query('a AND (b OR c)')

    assert node.node_type is FilterNodeType.AND
    assert [child.node_type for child in node.children or []] == [
        FilterNodeType.TEXT_SEARCH,
        FilterNodeType.OR,
    ]


def test_not_negates_the_term_after_it() -> None:
    node = parse_query('ERROR AND NOT level:debug')

    negated = (node.children or [])[1]
    assert negated.node_type is FilterNodeType.NOT
    assert (negated.children or [])[0].node_type is FilterNodeType.JSON_FIELD_EQUALS


def test_lowercase_or_is_still_text_because_log_lines_say_it() -> None:
    """Making `or` an operator would quietly change what an existing search means."""
    node = parse_query('timed out or retried')

    assert node.node_type is FilterNodeType.AND
    assert [child.value for child in node.children or []] == ['timed', 'out', 'or', 'retried']


def test_a_quoted_phrase_and_a_json_filter_survive_their_own_whitespace() -> None:
    node = parse_query('"connection timeout" OR { $.status >= 500 }')

    assert node.node_type is FilterNodeType.OR
    kinds = [child.node_type for child in node.children or []]
    assert kinds == [FilterNodeType.EXACT_PHRASE, FilterNodeType.JSON_FIELD_NUMERIC]


def test_an_empty_filter_matches_everything() -> None:
    assert parse_query('   ').node_type is FilterNodeType.MATCH_ALL


def test_the_round_trip_through_filter_to_string_holds() -> None:
    assert filter_to_string(parse_query('level:error OR status:>=500'))


@pytest.mark.parametrize(
    ('text', 'expected_in_suggestion'),
    [
        ('{ $..level = "x" }', 'one dot per level'),
        ('"unclosed', 'second "'),
        ('{ $.level = "x"', 'JSON filter looks like'),
        ('ERROR AND', 'needs a term after it'),
        ('OR ERROR', 'term before the operator'),
        ('(a OR b', 'matching )'),
        ('a OR b)', 'stray )'),
    ],
)
def test_a_malformed_filter_names_the_fix(text: str, expected_in_suggestion: str) -> None:
    """These surfaced as bare ValueError text with nothing to act on."""
    with pytest.raises(FilterParseError) as caught:
        parse_query(text)

    assert expected_in_suggestion in str(caught.value)


@pytest.mark.parametrize(
    ('text', 'pattern'),
    [
        ('ERROR', 'ERROR'),
        ('"internal server error"', '"internal server error"'),
        ('%[Ee]rror%', '%[Ee]rror%'),
        ('ERROR ARGUMENTS', 'ERROR ARGUMENTS'),
        ('ERROR OR WARNING', '?ERROR ?WARNING'),
        ('ERROR AND NOT ARGUMENTS', 'ERROR -ARGUMENTS'),
        ('level:error', '{ $.level = "error" }'),
        ('status:>=500', '{ $.status >= 500 }'),
        ('user.id:*', '{ $.user.id = * }'),
        ('NOT level:debug', '{ $.level != "debug" }'),
    ],
)
def test_what_cloudwatch_can_be_told_exactly_is_translated(text: str, pattern: str) -> None:
    assert portable_filter_pattern(parse_query(text)).pattern == pattern


def test_a_json_tree_keeps_its_own_and_or_and_parentheses() -> None:
    """CloudWatch has real && and || inside braces, unlike its text patterns."""
    portable = portable_filter_pattern(parse_query('level:error AND (status:>=500 OR status:404)'))

    assert portable.pattern == '{ ($.level = "error") && (($.status >= 500) || ($.status = "404")) }'


@pytest.mark.parametrize(
    ('text', 'expected_in_reason'),
    [
        ('ERROR OR level:debug', 'only or plain text terms'),
        ('ERROR AND level:debug', 'cannot and text terms with anything else'),
        ('NOT ERROR', 'no not over text'),
        ('', 'matches everything'),
    ],
)
def test_what_cloudwatch_would_get_wrong_is_refused_by_name(text: str, expected_in_reason: str) -> None:
    """The mixed case matters most: CloudWatch ignores its ?terms rather than failing."""
    portable = portable_filter_pattern(parse_query(text))

    assert portable.pattern is None
    assert expected_in_reason in portable.reason.lower()
