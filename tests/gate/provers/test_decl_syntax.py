"""Tests for `gate.provers.decl_syntax` — the shared prefix-aware
declaration-line scanner.
"""

from __future__ import annotations

import re

import pytest

from gate.indexer.extract import extract_declarations
from gate.inventory.scan import scan_text
from gate.provers.decl_syntax import (
    decl_line_regex,
    decl_line_regex_for,
    decl_name_from,
    decl_prefix_fragment,
    decl_target_fragment,
    normalize_decl_name,
    qualify_by_scope,
)
from gate.provers.isabelle import ISABELLE, qualify_isabelle_decl_names
from gate.provers.lean4 import LEAN4
from gate.provers.rocq import ROCQ
from gate.verify.statement_immutability import (
    Verdict,
    compare_declarations,
    count_base_declarations,
)
from gate.verify.style import find_decl_spans


def test_attribute_on_the_same_line_is_a_declaration_start() -> None:
    """The C1 false-block: `@[simp] theorem` was not a boundary, so the
    preceding declaration's span swallowed it."""
    rx = decl_line_regex(("theorem", "structure"), modifiers=("private",), attribute=("@[", "]"))
    m = rx.match("@[simp] theorem px : (1:Nat) = 1 := by rfl")
    assert m is not None
    assert m.group(1) == "theorem"
    assert m.group(2) == "px"


def test_modifier_prefixed_declaration_is_found() -> None:
    """The I2 fail-open: `private theorem` produced no span at all, so
    retyping it was invisible."""
    rx = decl_line_regex(("theorem", "structure"), modifiers=("private",))
    m = rx.match("private theorem foo : (1:Nat) = 1 := by rfl")
    assert m is not None
    assert m.group(1) == "theorem"
    assert m.group(2) == "foo"


def test_stacked_prefixes() -> None:
    """`@[simp] private theorem foo` and `Local Program Definition bar`."""
    lean_rx = decl_line_regex(
        ("theorem",), modifiers=("private", "protected"), attribute=("@[", "]")
    )
    m = lean_rx.match("@[simp] private theorem foo : (1:Nat) = 1 := by rfl")
    assert m is not None
    assert m.group(1) == "theorem"
    assert m.group(2) == "foo"

    rocq_rx = decl_line_regex(("Definition",), modifiers=("Local", "Global", "Program"))
    m2 = rocq_rx.match("Local Program Definition bar : nat := 0.")
    assert m2 is not None
    assert m2.group(1) == "Definition"
    assert m2.group(2) == "bar"


def test_bare_keyword_still_matches_unchanged() -> None:
    """No prefix present: identical behaviour to the old regex."""
    rx = decl_line_regex(("theorem", "def"))
    m = rx.match("theorem foo : (1:Nat) = 1 := by rfl")
    assert m is not None
    assert m.group(1) == "theorem"
    assert m.group(2) == "foo"


def test_a_modifier_alone_is_not_a_declaration() -> None:
    """`private` on its own line, or `Local` with no keyword after it,
    must not match — a false declaration start is worse than a miss,
    because it invents a span boundary."""
    rx = decl_line_regex(("theorem",), modifiers=("private",))
    assert rx.match("private") is None
    assert rx.match("private\n") is None

    rocq_rx = decl_line_regex(("Definition",), modifiers=("Local",))
    assert rocq_rx.match("Local") is None
    assert rocq_rx.match("Local.") is None


def test_keyword_appearing_as_a_prefix_word_is_not_swallowed() -> None:
    """A prover whose modifier list and keyword list overlap must still
    report the keyword in group 1. Construct the case rather than
    assuming none exists."""
    rx = decl_line_regex(("scoped", "theorem"), modifiers=("scoped",))

    # Used alone, as a keyword: not consumed as a modifier that leaves no
    # keyword left to match.
    m = rx.match("scoped foo : Nat := rfl")
    assert m is not None
    assert m.group(1) == "scoped"
    assert m.group(2) == "foo"

    # Used as a modifier stacked in front of a real keyword: still
    # reports that keyword, not the modifier.
    m2 = rx.match("scoped theorem foo : Nat := rfl")
    assert m2 is not None
    assert m2.group(1) == "theorem"
    assert m2.group(2) == "foo"


def test_name_normalization_is_unchanged() -> None:
    """Table from the previous slice: foo: -> foo, foo[simp]: -> foo,
    foo.bar: -> foo.bar, ':' -> ':' (anonymous forms keep the raw token)."""
    assert normalize_decl_name("foo:") == "foo"
    assert normalize_decl_name("foo[simp]:") == "foo"
    assert normalize_decl_name("foo.bar:") == "foo.bar"
    assert normalize_decl_name(":") == ":"


# ---------------------------------------------------------------------------
# qualify_by_scope — the shared scope-path DISAMBIGUATION key
# (statement-immutability hardening round 4, F2).
#
# Exercised here against synthetic openers/closers rather than any real
# prover's, so these tests pin the helper's own contract; the per-prover
# wiring is pinned in test_rocq.py / test_isabelle.py.
# ---------------------------------------------------------------------------

_DECL = decl_line_regex(("thm",))
_OPEN = re.compile(r"^\s*scope\s+(\w+)")
_PLAIN = re.compile(r"^\s*group\b")
_CLOSE_NAMED = re.compile(r"^\s*close\s+(\w+)\s*$")
_CLOSE_BARE = re.compile(r"^\s*close\s*$")
_COMMIT = re.compile(r"\bopen\b")


def test_scope_path_prefixes_and_pops() -> None:
    src = "scope A\nthm x\nclose A\nthm y\n"
    assert qualify_by_scope(
        src, decl_re=_DECL, qualifying_open_re=_OPEN, close_re=_CLOSE_NAMED
    ) == {2: "A.x", 4: "y"}


def test_scope_path_nests() -> None:
    src = "scope A\nscope B\nthm x\nclose B\nthm y\nclose A\n"
    assert qualify_by_scope(
        src, decl_re=_DECL, qualifying_open_re=_OPEN, close_re=_CLOSE_NAMED
    ) == {3: "A.B.x", 5: "A.y"}


def test_plain_open_is_tracked_without_contributing() -> None:
    """A non-qualifying scope must not prefix the name, but must still
    absorb its own closer — otherwise that closer pops the enclosing
    qualifying scope and everything after it silently loses its
    prefix."""
    src = "scope A\ngroup\nthm x\nclose\nthm y\nclose\n"
    assert qualify_by_scope(
        src,
        decl_re=_DECL,
        qualifying_open_re=_OPEN,
        plain_open_re=_PLAIN,
        close_re=_CLOSE_BARE,
    ) == {3: "A.x", 5: "A.y"}


def test_commit_defers_the_push_to_a_later_line() -> None:
    """The two-step opener (isabelle's `locale … begin`): the opener
    arms a pending name and `commit_re` pushes it, so a declaration
    between the two is NOT yet in the scope."""
    src = "scope A\nthm outside\nopen\nthm inside\nclose A\n"
    assert qualify_by_scope(
        src,
        decl_re=_DECL,
        qualifying_open_re=_OPEN,
        close_re=_CLOSE_NAMED,
        commit_re=_COMMIT,
    ) == {2: "outside", 4: "A.inside"}


def test_commit_on_the_opener_line_itself() -> None:
    """`locale A begin` on one line must both arm and commit, which is
    why the opener branch falls through to the commit check instead of
    continuing straight to the next line."""
    src = "scope A open\nthm x\nclose A\n"
    assert qualify_by_scope(
        src,
        decl_re=_DECL,
        qualifying_open_re=_OPEN,
        close_re=_CLOSE_NAMED,
        commit_re=_COMMIT,
    ) == {2: "A.x"}


def test_unmatched_closer_pops_nothing_rather_than_raising() -> None:
    """Every prover has `begin`/`End` constructs this helper does not
    open (an isabelle theory's own wrapper, most obviously), so a
    closer with no matching opener must be a no-op on ordinary input."""
    src = "close\nthm x\n"
    assert qualify_by_scope(
        src, decl_re=_DECL, qualifying_open_re=_OPEN, close_re=_CLOSE_BARE
    ) == {2: "x"}


def test_out_of_order_named_close_removes_the_named_scope() -> None:
    """Same tolerance as lean4's `_pop_innermost`: a named closer
    removes the scope it names even when that is not the innermost one,
    so malformed input still converges instead of drifting."""
    src = "scope A\nscope B\nclose A\nthm x\n"
    assert qualify_by_scope(
        src, decl_re=_DECL, qualifying_open_re=_OPEN, close_re=_CLOSE_NAMED
    ) == {4: "B.x"}


def test_names_are_normalized_like_span_names() -> None:
    """The keys have to agree with the span names
    `gate.verify.style.find_decl_spans` produces from the same regex, so
    the trailing-colon normalization must be applied here too — an Isar
    `lemma foo: "P"` captures `foo:` as its raw token."""
    src = 'scope A\nthm foo: "P"\nclose A\n'
    assert qualify_by_scope(
        src, decl_re=_DECL, qualifying_open_re=_OPEN, close_re=_CLOSE_NAMED
    ) == {2: "A.foo"}


def test_empty_text_yields_no_keys() -> None:
    assert (
        qualify_by_scope(
            "", decl_re=_DECL, qualifying_open_re=_OPEN, close_re=_CLOSE_NAMED
        )
        == {}
    )


# ---------------------------------------------------------------------------
# An opener that is ALSO a declaration (round 7, F2).
#
# isabelle's `locale`/`class` carry `assumes` clauses that every theorem
# in the scope depends on, so the header has to be compared like any
# other declaration — which makes the opening line both an opener and a
# declaration. This helper used to treat the two as exclusive, and the
# isabelle profile recorded that as the reason a scope command could not
# be enumerated.
# ---------------------------------------------------------------------------

_DECL_WITH_SCOPE = decl_line_regex(("thm", "scope"))


def test_an_opener_that_is_also_a_declaration_gets_its_own_key() -> None:
    src = "scope A\nthm x\nclose A\n"
    assert qualify_by_scope(
        src,
        decl_re=_DECL_WITH_SCOPE,
        qualifying_open_re=_OPEN,
        close_re=_CLOSE_NAMED,
    ) == {1: "A", 2: "A.x"}


def test_an_opener_declaration_keys_under_its_enclosing_scope() -> None:
    """Not under itself: `scope B` inside `scope A` is `A.B`, never
    `A.B.B`. The key is computed before the push for exactly this."""
    src = "scope A\nscope B\nthm x\nclose B\nclose A\n"
    assert qualify_by_scope(
        src,
        decl_re=_DECL_WITH_SCOPE,
        qualifying_open_re=_OPEN,
        close_re=_CLOSE_NAMED,
    ) == {1: "A", 2: "A.B", 3: "A.B.x"}


def test_an_opener_declaration_keys_before_a_deferred_commit() -> None:
    """With a two-step opener the key still lands on the opener's line,
    not the `commit_re` line that pushes the scope."""
    src = "scope A\nopen\nthm x\nclose A\n"
    assert qualify_by_scope(
        src,
        decl_re=_DECL_WITH_SCOPE,
        qualifying_open_re=_OPEN,
        close_re=_CLOSE_NAMED,
        commit_re=_COMMIT,
    ) == {1: "A", 3: "A.x"}


def test_an_opener_that_is_not_a_declaration_keyword_gains_no_key() -> None:
    """Inertness for rocq, whose `Module`/`Section` are deliberately
    absent from its `_DECL_KEYWORDS`, and for isabelle's `context`,
    which scopes declarations without declaring anything itself."""
    src = "scope A\nthm x\nclose A\n"
    assert qualify_by_scope(
        src, decl_re=_DECL, qualifying_open_re=_OPEN, close_re=_CLOSE_NAMED
    ) == {2: "A.x"}


def test_a_plain_opener_that_is_also_a_declaration_gets_an_enclosing_key() -> None:
    """A non-contributing scope's own key carries only the prefix of
    whatever encloses it — never its own name, which it does not
    contribute to anything."""
    plain_decl = decl_line_regex(("thm", "group"))
    src = "scope A\ngroup G\nthm x\nclose\nclose A\n"
    assert qualify_by_scope(
        src,
        decl_re=plain_decl,
        qualifying_open_re=_OPEN,
        plain_open_re=_PLAIN,
        close_re=_CLOSE_BARE,
    ) == {2: "A.G", 3: "A.x"}


# ---------------------------------------------------------------------------
# prefix_flags + decl_line_regex_for — the fourth prefix shape and the
# one-place argument list (statement-immutability hardening round 6, F1).
# ---------------------------------------------------------------------------


def test_prefix_flag_with_a_terminated_argument_is_a_declaration_start() -> None:
    """The fourth shape: a prefix keyword taking an argument, with no
    `in` to close it and no bracket to balance. Rocq's `control_flag`s
    are the live instance."""
    rx = decl_line_regex(
        ("Definition", "Theorem"),
        prefix_flags=("Time", r"Timeout\s+[0-9]+"),
    )
    m = rx.match("Timeout 10 Theorem t : 1 = 1.")
    assert m is not None
    assert (m.group(1), m.group(2)) == ("Theorem", "t")
    # Flags stack (`LIST0 control_flag`) and interleave with the other
    # shapes, because the shared alternation is order-free.
    stacked = decl_line_regex(
        ("Definition",),
        modifiers=("Local",),
        prefix_flags=("Time", r"Timeout\s+[0-9]+"),
    ).match("Time Timeout 5 Local Definition foo := 5.")
    assert stacked is not None
    assert stacked.group(2) == "foo"


def test_prefix_flag_fragment_with_a_capturing_group_is_rejected() -> None:
    """`decl_line_regex` promises group 1 = keyword, group 2 = name.

    `prefix_flags` entries are raw regex (the argument's token shape is
    prover syntax, so it lives in the prover module), which makes a
    stray capturing group a way to silently renumber both — so the
    builder refuses one rather than trusting the caller.
    """
    try:
        decl_prefix_fragment((), None, prefix_flags=(r"Timeout\s+([0-9]+)",))
    except ValueError as exc:
        assert "capturing group" in str(exc)
    else:  # pragma: no cover - the assertion below is the failure path
        raise AssertionError("a capturing group in a prefix flag must be rejected")

    # A non-capturing group is fine.
    assert decl_prefix_fragment((), None, prefix_flags=(r"Timeout\s+(?:[0-9]+)",))


def test_no_prefix_shapes_still_returns_an_empty_fragment() -> None:
    """A profile that declares none of the four shapes must get exactly
    the old unprefixed pattern back."""
    assert decl_prefix_fragment((), None) == ""
    assert decl_prefix_fragment((), None, prefix_commands=(), prefix_flags=()) == ""


def test_decl_line_regex_for_reads_every_prefix_shape_off_the_profile() -> None:
    """Each `gate/` enumeration consumer used to spell the prefix
    argument list out by hand, so every new shape had to be threaded to
    all of them in lockstep — and a missed site does not fail loudly, it
    silently keeps that shape's fail-open at that one consumer. This is
    the shape-completeness check that replaces that discipline.
    """
    lean_rx = decl_line_regex_for(LEAN4)
    assert lean_rx.match("@[simp] private theorem foo : True := trivial") is not None
    assert lean_rx.match("open Nat in theorem bar : True := trivial") is not None

    rocq_rx = decl_line_regex_for(ROCQ)
    assert rocq_rx.match("Timeout 10 Theorem t : 1 = 1.") is not None
    assert rocq_rx.match("#[global] Local Definition foo := 5.") is not None

    assert decl_line_regex_for(ISABELLE).match('private lemma foo: "P"') is not None

    # `keywords` narrows the kind set without dropping the prefixes.
    narrowed = decl_line_regex_for(ROCQ, keywords=("Theorem",))
    assert narrowed.match("Time Theorem t : 1 = 1.") is not None
    assert narrowed.match("Time Definition foo := 5.") is None


def test_every_prefix_shape_reaches_all_three_enumeration_consumers() -> None:
    """The three name-keyed consumers of the boundary regex all go
    through `decl_line_regex_for`, so a control-flagged rocq
    declaration is visible to each of them — not just to the
    statement-immutability audit that motivated the fix."""
    source = "Time Definition foo := 5.\nAdmitted.\n"

    spans = find_decl_spans(source, profile=ROCQ)
    assert [s.name for s in spans] == ["foo"]

    _axioms, sorries = scan_text(source, "a.v", profile=ROCQ)
    assert [s.decl for s in sorries] == ["foo"]

    decls = extract_declarations(source, "a.v", profile=ROCQ)
    assert [d.name for d in decls] == ["foo"]


# ---------------------------------------------------------------------------
# decl_target_fragment — the fifth shape, and the only POST-keyword one
# (round 8, F1). Isabelle's `lemma (in A) foo:`.
# ---------------------------------------------------------------------------


def test_target_group_is_skipped_and_the_name_still_lands_in_group_2() -> None:
    """The group sits BETWEEN keyword and name, so the contract is at risk.

    `decl_line_regex` promises group 1 = keyword and group 2 = name; a
    capturing group here would renumber the name specifically, which is
    why `decl_target_fragment` refuses one (below).
    """
    rx = decl_line_regex(("lemma", "definition"), target=r"\(\s*in\b\s*[^)\s]+\s*\)")
    m = rx.match('lemma (in A) foo: "P x"')
    assert m is not None
    assert (m.group(1), m.group(2)) == ("lemma", "foo:")
    # Untargeted declarations are unaffected — the group is optional.
    plain = rx.match('lemma bar: "P"')
    assert plain is not None
    assert plain.group(2) == "bar:"


def test_target_group_is_optional_so_a_line_ending_in_it_still_matches() -> None:
    """The backtracking property, which is load-bearing rather than incidental.

    `lemma (in A)` with the name on the FOLLOWING line is legal Isabelle
    (73 declaration lines in the Isabelle2025-2 distribution are written
    that way). A non-optional group would fail to match such a line
    altogether, deleting the declaration from enumeration — the
    fail-open this module exists to close. Optional means the engine
    backtracks and captures the old raw token instead.
    """
    rx = decl_line_regex(("lemma",), target=r"\(\s*in\b\s*[^)\s]+\s*\)")
    m = rx.match("lemma (in A)")
    assert m is not None
    assert m.group(2) == "(in"


def test_target_fragment_with_a_capturing_group_is_rejected() -> None:
    """Same rule as `prefix_flags`, enforced rather than trusted."""
    try:
        decl_target_fragment(r"\(\s*in\s+(\S+)\)")
    except ValueError as exc:
        assert "capturing group" in str(exc)
    else:  # pragma: no cover - the assertion below is the failure path
        raise AssertionError("a capturing group in the target must be rejected")

    assert decl_target_fragment(r"\(\s*in\s+(?:\S+)\)")


def test_no_target_returns_an_empty_fragment() -> None:
    """A profile that sets nothing gets exactly the pre-round-8 pattern."""
    assert decl_target_fragment(None) == ""
    assert decl_line_regex(("lemma",)).pattern == decl_line_regex(
        ("lemma",), target=None
    ).pattern


def test_decl_line_regex_for_reads_the_target_off_the_profile() -> None:
    """The fifth shape joins the one-place argument list, so all three
    enumeration consumers get it without threading it by hand."""
    isabelle_match = decl_line_regex_for(ISABELLE).match('lemma (in A) foo: "P"')
    assert isabelle_match is not None
    assert normalize_decl_name(isabelle_match.group(2)) == "foo"
    # Inert for the two profiles that set no target: they still match the
    # line (they always did) but do not skip the group, so the name is
    # the raw `(in` token exactly as before.
    lean_match = decl_line_regex_for(LEAN4).match("theorem (in A) foo : True")
    assert lean_match is not None and lean_match.group(2) == "(in"
    rocq_match = decl_line_regex_for(ROCQ).match("Theorem (in A) foo : True.")
    assert rocq_match is not None and rocq_match.group(2) == "(in"


def test_the_target_reaches_all_three_enumeration_consumers() -> None:
    """Same completeness check the fourth shape got, for the fifth."""
    source = 'lemma (in A) foo: "P"\n  sorry\n'

    assert [s.name for s in find_decl_spans(source, profile=ISABELLE)] == ["foo"]

    _axioms, sorries = scan_text(source, "T.thy", profile=ISABELLE)
    assert [s.decl for s in sorries] == ["foo"]

    decls = extract_declarations(source, "T.thy", profile=ISABELLE)
    assert [d.name for d in decls] == ["foo"]


# ---------------------------------------------------------------------------
# qualify_by_scope's `decl_target_re` — an inline scope name (round 8, F1).
# ---------------------------------------------------------------------------

_TARGET_KEY = re.compile(r"^\s*(?:thm)\s+\(\s*in\b\s*(?:-|([A-Za-z_][\w'.]*))\s*\)")

# The boundary regex must skip the same group the key regex reads, or
# group 2 is the literal `(in` — which is the defect, not the fix.
_DECL_TARGETED = decl_line_regex(
    ("thm", "scope"), target=r"\(\s*in\b\s*[^)\s]+\s*\)"
)


def test_inline_target_replaces_the_enclosing_scope_path() -> None:
    """An immediate target SUSPENDS the enclosing context, so it replaces
    rather than extends the path (Isar reference manual §5.2)."""
    text = "scope A\nthm (in B) x\nthm plain\nclose A\n"
    keys = qualify_by_scope(
        text,
        decl_re=_DECL_TARGETED,
        qualifying_open_re=_OPEN,
        close_re=_CLOSE_NAMED,
        decl_target_re=_TARGET_KEY,
    )
    assert keys == {1: "A", 2: "B.x", 3: "A.plain"}


def test_inline_target_with_no_group_match_means_the_global_scope() -> None:
    """A matched pattern whose group 1 did not participate is the
    prover's global-target spelling (isabelle writes it `(in -)`), and
    keys as the bare name whatever encloses it."""
    text = "scope A\nthm (in -) g\nclose A\n"
    keys = qualify_by_scope(
        text,
        decl_re=_DECL_TARGETED,
        qualifying_open_re=_OPEN,
        close_re=_CLOSE_NAMED,
        decl_target_re=_TARGET_KEY,
    )
    assert keys == {1: "A", 2: "g"}


def test_an_unrecognised_target_spelling_falls_back_to_the_stack() -> None:
    """Degrade to the coarser key, never to a wrong one.

    A key that disagrees between base and head is worse than a coarse
    one, so a target the pattern does not recognise must leave the
    enclosing-scope path alone rather than guess. The boundary regex is
    deliberately looser than the key regex (it skips any single
    whitespace-free token), so the NAME is still right — only the path
    falls back.
    """
    text = "scope A\nthm (in 5) n\nclose A\n"
    keys = qualify_by_scope(
        text,
        decl_re=_DECL_TARGETED,
        qualifying_open_re=_OPEN,
        close_re=_CLOSE_NAMED,
        decl_target_re=_TARGET_KEY,
    )
    assert keys == {1: "A", 2: "A.n"}


def test_omitting_decl_target_re_is_byte_identical_to_before() -> None:
    """Inert for rocq and for isabelle's own pre-round-8 behaviour."""
    text = "scope A\nthm x\nclose A\n"
    common = {
        "decl_re": _DECL_WITH_SCOPE,
        "qualifying_open_re": _OPEN,
        "close_re": _CLOSE_NAMED,
    }
    assert qualify_by_scope(text, **common) == qualify_by_scope(
        text, **common, decl_target_re=_TARGET_KEY
    )


# ---------------------------------------------------------------------------
# Round 11, F3: the unenumerated population.
#
# Three shapes, all measured over the Isabelle2025-2 sources. The first
# two were fail-opens — `decl_line_regex` matched nothing, so the
# declaration was absent from every enumeration built on it and its
# statement could be rewritten freely. The third was a mis-key.
# ---------------------------------------------------------------------------


def test_keyword_suffix_marker_percent_and_paren_group() -> None:
    rx = decl_line_regex_for(ISABELLE)
    cases = {
        "definition\\<^marker>\\<open>tag important\\<close> foo :: nat where":
            ("definition", "foo"),
        "corollary\\<^marker>\\<open>tag unimportant\\<close> cor1: \"P\"":
            ("corollary", "cor1"),
        "lift_definition(code_dt) upair_inv :: nat is x": ("lift_definition", "upair_inv"),
        "theorem%important thm1: \"P\"": ("theorem", "thm1"),
        "consts %quote f :: nat": ("consts", "f"),
    }
    for line, (keyword, name) in cases.items():
        match = rx.match(line)
        assert match is not None, line
        assert match.group(1) == keyword, line
        assert normalize_decl_name(match.group(2)) == name, line


def test_keyword_suffix_does_not_swallow_the_locale_target() -> None:
    """`(in A)` must stay the TARGET, not a keyword-suffix option group.

    `qualify_by_scope` reads the target as a scope key, so a
    keyword-suffix match there would silently lose the locale
    qualification.
    """
    rx = decl_line_regex_for(ISABELLE)
    match = rx.match('lemma (in A) foo: "P"')
    assert match is not None
    assert normalize_decl_name(match.group(2)) == "foo"
    assert qualify_isabelle_decl_names('lemma (in A) foo: "P"\n') == {1: "A.foo"}


def test_type_params_before_the_name_resolve_to_the_name() -> None:
    rx = decl_line_regex_for(ISABELLE)
    cases = {
        "datatype ('a, 'b) t = A | B": "t",
        "datatype 'a tree = Leaf": "tree",
        "record 'a ring = \"'a monoid\"": "ring",
        "quotient_type 'a myoption = \"'a + 'a\" / r": "myoption",
        'typedef (overloaded) (\'a, \'b :: len) vec = "{xs. True}"': "vec",
        "primrec (nonexhaustive) f :: nat where": "f",
    }
    for line, name in cases.items():
        match = rx.match(line)
        assert match is not None, line
        assert normalize_decl_name(match.group(2)) == name, line


def test_type_params_leave_an_ordinary_declaration_alone() -> None:
    rx = decl_line_regex_for(ISABELLE)
    for line, name in (
        ('definition foo :: nat where "foo = 5"', "foo"),
        ('lemma foo: "P"', "foo"),
        ('lemma "P x"', '"P'),
        ('lemma [code]: "f = g"', "[code]:"),
        ('definition "restrict" :: nat', '"restrict"'),
    ):
        match = rx.match(line)
        assert match is not None, line
        assert normalize_decl_name(match.group(2)) == name, line


def test_keyword_alone_on_its_line_matches_with_no_name_group() -> None:
    """The largest of the three: 3 784 isabelle declaration lines.

    `CCL/Gfp.thy:12` is `definition` with `  gfp :: "…" where …` on the
    next line. A line-local `\\s+(\\S+)` matched none of these, so the
    declaration was unenumerated and `count_base_declarations` never saw
    it.
    """
    rx = decl_line_regex_for(ISABELLE)
    match = rx.match("definition")
    assert match is not None
    assert match.group(1) == "definition"
    assert match.group(2) is None


def test_decl_name_from_resolves_a_name_on_the_following_line() -> None:
    rx = decl_line_regex_for(ISABELLE)
    lines = ["definition", '  gfp :: "[\'a set => \'a set] => \'a set" where',
             '  "gfp(f) == Union({u. u <= f(u)})"']
    match = rx.match(lines[0])
    assert match is not None
    assert decl_name_from(match, lines, 0) == "gfp"


def test_decl_name_from_skips_blank_lines() -> None:
    rx = decl_line_regex_for(ISABELLE)
    lines = ["consts", "", "  cps_of_set :: nat"]
    match = rx.match(lines[0])
    assert match is not None
    assert decl_name_from(match, lines, 0) == "cps_of_set"


def test_decl_name_from_yields_a_junk_key_where_there_is_no_name() -> None:
    """Named rather than hidden: this converts a fail-open into a block.

    `theorem` / `  fixes M::…` has no name to recover, so the key is
    `fixes`. That is a duplicate-name UNDETERMINED — a false block —
    where before it was an unenumerated declaration whose statement
    could be rewritten invisibly. The direction is the point.
    """
    rx = decl_line_regex_for(ISABELLE)
    for following, key in (
        ("  fixes M :: nat", "fixes"),
        ('  [code]: "f = g"', "[code]:"),
        ("  where", "where"),
    ):
        lines = ["theorem", following]
        match = rx.match(lines[0])
        assert match is not None
        assert decl_name_from(match, lines, 0) == key, following


def test_decl_name_from_falls_back_to_the_keyword_at_eof() -> None:
    rx = decl_line_regex_for(ISABELLE)
    lines = ["definition", "   ", ""]
    match = rx.match(lines[0])
    assert match is not None
    assert decl_name_from(match, lines, 0) == "definition"


def test_declaration_with_the_name_on_the_next_line_is_enumerated() -> None:
    src = (
        "definition\n"
        '  gfp :: "nat" where\n'
        '  "gfp = 5"\n'
        "\n"
        'lemma other: "True"\n'
        "  by simp\n"
    )
    spans = find_decl_spans(src, profile=ISABELLE)
    assert [(s.keyword, s.name) for s in spans] == [
        ("definition", "gfp"),
        ("lemma", "other"),
    ]


def test_next_line_name_closes_the_statement_rewrite_fail_open() -> None:
    base = "definition\n  gfp :: nat where\n  \"gfp = 5\"\n"
    head = "definition\n  gfp :: int where\n  \"gfp = 5\"\n"
    assert count_base_declarations(base, profile=ISABELLE) == 1
    verdict, findings = compare_declarations(base, head, profile=ISABELLE)
    assert verdict is Verdict.CHANGED
    assert [f.decl for f in findings] == ["gfp"]


def test_keyword_suffix_and_type_params_reject_capturing_groups() -> None:
    with pytest.raises(ValueError, match="capturing group"):
        decl_line_regex(("datatype",), keyword_suffix=r"(%\w+)")
    with pytest.raises(ValueError, match="capturing group"):
        decl_line_regex(("datatype",), type_params=r"('\w+)")


def test_lean4_and_rocq_are_unaffected_by_the_two_new_fragments() -> None:
    for profile in (LEAN4, ROCQ):
        assert profile.decl_keyword_suffix is None
        assert profile.decl_type_params_syntax is None
