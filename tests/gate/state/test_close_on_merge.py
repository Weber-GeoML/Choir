"""Tests for `gate.state.close_on_merge.parse_closing_refs`."""

from __future__ import annotations

from gate.state.close_on_merge import parse_closing_refs


def test_every_closing_keyword_spelling_is_read() -> None:
    for body in (
        "Closes #42",
        "closes #42",
        "CLOSES #42",
        "Fixes #42",
        "Resolves #42",
        "This PR closed #42",
        "Adds the new theorem `one_add_one`. Closes #42 and fixes #43.\n",
    ):
        assert 42 in parse_closing_refs(body), body


def test_a_non_reference_closes_nothing() -> None:
    for body in (
        "",
        "just some text\nno issue references here\n",
        "Closes#42",            # no whitespace: not a GitHub closing keyword
        "close the issue #42",  # the keyword must be adjacent to #N
        "Closes #abc",
        "closesfoo #42",        # the keyword must end on a word boundary
    ):
        assert parse_closing_refs(body) == [], body


def test_refs_are_deduped_and_ordered_by_first_occurrence() -> None:
    assert parse_closing_refs("Closes #5\nCloses #1\nFixes #5\nResolves #3") == [5, 1, 3]


def test_several_issues_in_one_sentence_all_close() -> None:
    body = "Closes #10, fixes #20, resolves #1234567"
    assert parse_closing_refs(body) == [10, 20, 1234567]
