"""Tests for `gate.provers` — the profile dataclasses and registry."""

from __future__ import annotations

import dataclasses

import pytest

from gate.provers import PROFILES, ProverError, get_profile
from gate.provers.base import CommentSyntax, TrustEntry
from gate.provers.lean4 import LEAN4
from gate.verify.axiom_honesty import _PATTERNS as _AXIOM_HONESTY_PATTERNS

# ---------------------------------------------------------------------------
# CommentSyntax / TrustEntry
# ---------------------------------------------------------------------------


def test_comment_syntax_nested_defaults_true() -> None:
    cs = CommentSyntax(line="--", block_open="/-", block_close="-/")
    assert cs.nested is True


def test_comment_syntax_is_frozen() -> None:
    cs = CommentSyntax(line="--", block_open="/-", block_close="-/")
    with pytest.raises(dataclasses.FrozenInstanceError):
        cs.line = "#"  # type: ignore[misc]


def test_comment_syntax_line_none_for_block_only_provers() -> None:
    cs = CommentSyntax(line=None, block_open="(*", block_close="*)")
    assert cs.line is None


def test_trust_entry_clean_true_means_no_assumptions() -> None:
    entry = TrustEntry(decl="foo", assumptions=(), clean=True)
    assert entry.assumptions == ()
    assert entry.clean is True


def test_trust_entry_is_frozen() -> None:
    entry = TrustEntry(decl="foo", assumptions=(), clean=True)
    with pytest.raises(dataclasses.FrozenInstanceError):
        entry.decl = "bar"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ProverProfile is frozen
# ---------------------------------------------------------------------------


def test_prover_profile_is_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        LEAN4.name = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Registry invariants (design note 12 §10)
# ---------------------------------------------------------------------------


def test_registry_has_lean4_isabelle_and_rocq() -> None:
    assert set(PROFILES) == {"lean4", "isabelle", "rocq"}
    assert PROFILES["lean4"] is LEAN4


def test_every_profile_has_nonempty_extensions() -> None:
    for profile in PROFILES.values():
        assert profile.file_extensions, profile.name


def test_every_extension_starts_with_dot() -> None:
    for profile in PROFILES.values():
        for ext in profile.file_extensions:
            assert ext.startswith("."), (profile.name, ext)


def test_every_profile_has_nonempty_build_command() -> None:
    for profile in PROFILES.values():
        assert profile.build_command, profile.name


def test_every_profile_supplies_a_declaration_qualifier() -> None:
    # The field is typed optional for consumers that can fall back to a
    # surface name, but `gate.verify.sorry_delta.compare_reduction`
    # cannot: its per-declaration count key and its refusal of a key
    # two declarations share are both computed from this hook. A profile
    # without one counts placeholders per surface name, pooling two
    # same-named declarations in sibling scopes into one count, and both
    # of those rules go inert.
    for profile in PROFILES.values():
        assert profile.qualify_decl_names is not None, profile.name


# ---------------------------------------------------------------------------
# get_profile
# ---------------------------------------------------------------------------


def test_get_profile_returns_lean4() -> None:
    assert get_profile("lean4") is LEAN4


def test_get_profile_unknown_raises_prover_error_listing_valid_names() -> None:
    with pytest.raises(ProverError, match="lean4"):
        get_profile("nope")


# ---------------------------------------------------------------------------
# lean4 defaults (note 12 §2.2/§3.1, verbatim)
# ---------------------------------------------------------------------------


def test_lean4_declarative_fields() -> None:
    assert LEAN4.name == "lean4"
    assert LEAN4.file_extensions == (".lean",)
    assert LEAN4.comment_syntax == CommentSyntax(
        line="--", block_open="/-", block_close="-/", nested=True
    )
    assert LEAN4.decl_keywords == (
        "theorem",
        "lemma",
        "def",
        "abbrev",
        "instance",
        "example",
        "structure",
        "class",
        "inductive",
        "axiom",
        "opaque",
    )
    assert LEAN4.placeholder_tokens == ("sorry",)
    assert LEAN4.build_command == ("lake", "build")
    assert LEAN4.toolchain_file == "lean-toolchain"
    assert LEAN4.protected_files == (
        "lakefile.toml",
        "lakefile.lean",
        "lean-toolchain",
        "lake-manifest.json",
    )
    assert LEAN4.extra_audits == ()
    assert LEAN4.search_tooling_note is True


def test_lean4_statement_keywords_is_narrower_than_decl_keywords() -> None:
    # `structure`/`class`/`inductive`/`axiom`/`opaque` have no
    # statement/proof-body split (spec D2,
    # gate.verify.statement_immutability) — the five `decl_keywords`
    # entries `extract_lean_statement` cannot resolve.
    assert LEAN4.statement_keywords == (
        "theorem",
        "lemma",
        "def",
        "abbrev",
        "instance",
        "example",
    )
    assert set(LEAN4.statement_keywords) < set(LEAN4.decl_keywords)


def test_lean4_trust_patterns_copied_verbatim_from_axiom_honesty() -> None:
    # trust_patterns must be an exact copy of gate.verify.axiom_honesty's
    # _PATTERNS (same labels, same regex source, same order). Assert
    # against the source of truth rather than retyping the regexes, so
    # a future edit to axiom_honesty.py's patterns surfaces here as a
    # mismatch instead of silent drift.
    expected = tuple(
        (name, pattern.pattern) for name, pattern in _AXIOM_HONESTY_PATTERNS.items()
    )
    assert LEAN4.trust_patterns == expected


def test_lean4_one_probe_per_decl_is_false() -> None:
    # lean4's `#print axioms` output names each declaration, so one
    # probe covers every target — see tests/gate/provers/test_trust.py
    # for the real trust-report hooks (design note 12 §4).
    assert LEAN4.one_probe_per_decl is False
