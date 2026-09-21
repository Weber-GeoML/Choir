"""Tests for `viz.gitscan`'s reading of declaration state from source text."""

from __future__ import annotations

from gate.provers.lean4 import LEAN4
from viz.gitscan import _file_decls


def test_an_anonymous_declaration_is_not_drawn_under_the_next_token() -> None:
    """The prover names these itself, and the plan records that name; a
    picture must not invent `(f` or `:` beside it."""
    text = (
        "instance : IsProbabilityMeasure fairCoin := trivial\n"
        "instance (f : I) : IsProbabilityMeasure (pi f) := trivial\n"
        "theorem named : True := trivial\n"
    )
    assert set(_file_decls(text, "P.lean", profile=LEAN4)) == {"named"}


def test_a_placeholder_is_read_against_the_declaration_it_sits_in() -> None:
    text = "theorem open_goal : True := sorry\ntheorem done : True := trivial\n"
    decls = _file_decls(text, "P.lean", profile=LEAN4)
    assert decls["open_goal"].has_placeholder
    assert not decls["done"].has_placeholder
    assert decls["done"].file == "P.lean"
