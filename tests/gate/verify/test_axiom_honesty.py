"""Tests for `gate.verify.axiom_honesty`."""

from __future__ import annotations

from gate.provers.isabelle import ISABELLE
from gate.provers.rocq import ROCQ
from gate.verify.axiom_honesty import (
    Verdict,
    compare,
    compile_patterns,
    count_patterns,
    extract_axiom_names,
    format_findings,
)
from gate.verify.config import AxiomHonestyConfig, AxiomPolicy

# ---------------------------------------------------------------------------
# count_patterns
# ---------------------------------------------------------------------------


def test_count_empty_file() -> None:
    assert count_patterns("") == {
        "axiom": 0,
        "unsafe": 0,
        "partial": 0,
        "native_decide": 0,
        "extern": 0,
    }


def test_count_axiom_declaration() -> None:
    src = "axiom choice : Nonempty α → α\n"
    assert count_patterns(src)["axiom"] == 1


def test_count_multiple_axioms() -> None:
    src = "axiom a : Nat\naxiom b : Nat\naxiom c : Nat\n"
    assert count_patterns(src)["axiom"] == 3


def test_count_axiom_in_identifier_not_matched() -> None:
    # `axiomatic_foo` shouldn't count as an axiom decl.
    src = "def axiomatic_foo : Nat := 42\n"
    assert count_patterns(src)["axiom"] == 0


def test_count_partial_decl() -> None:
    src = "partial def loop : Nat → Nat := fun n => loop (n+1)\n"
    assert count_patterns(src)["partial"] == 1


def test_count_partial_in_identifier_not_matched() -> None:
    # `partial_function` has `partial` inside an identifier (underscore is
    # a word char, so `\bpartial\b` doesn't match here).
    src = "def partial_function (n : Nat) : Nat := n\n"
    assert count_patterns(src)["partial"] == 0


def test_count_unsafe() -> None:
    src = "unsafe def cast (x : α) : β := unsafeCast x\n"
    assert count_patterns(src)["unsafe"] >= 1


def test_count_native_decide() -> None:
    src = "theorem foo : 2 + 2 = 4 := by native_decide\n"
    assert count_patterns(src)["native_decide"] == 1


def test_count_extern() -> None:
    src = '@[extern "c_function"] def foo : Nat := 0\n'
    assert count_patterns(src)["extern"] == 1


# ---------------------------------------------------------------------------
# compare
# ---------------------------------------------------------------------------


def test_compare_clean_when_both_empty() -> None:
    verdict, findings = compare("", "")
    assert verdict == Verdict.CLEAN
    assert findings == []


def test_compare_clean_when_unchanged() -> None:
    src = "axiom choice : Nonempty α → α\n"
    verdict, findings = compare(src, src)
    assert verdict == Verdict.CLEAN
    assert findings == []


def test_compare_flags_new_axiom() -> None:
    base = "theorem foo : 1 = 1 := rfl\n"
    head = base + "axiom bad : False\n"
    verdict, findings = compare(base, head)
    assert verdict == Verdict.INTRODUCED
    patterns = [f.pattern for f in findings]
    assert "axiom" in patterns


def test_compare_flags_new_native_decide() -> None:
    base = "theorem foo : 1 = 1 := rfl\n"
    head = "theorem foo : 1 = 1 := by native_decide\n"
    verdict, findings = compare(base, head)
    assert verdict == Verdict.INTRODUCED
    assert findings[0].pattern == "native_decide"
    assert findings[0].delta == 1


def test_compare_doesnt_flag_decrease() -> None:
    # Removing a previously-existing axiom isn't a flag — only net additions.
    base = "axiom a : Nat\naxiom b : Nat\n"
    head = "axiom a : Nat\n"
    verdict, findings = compare(base, head)
    assert verdict == Verdict.CLEAN
    assert findings == []


def test_compare_flags_multiple_patterns_together() -> None:
    base = "theorem foo : 1 = 1 := rfl\n"
    head = (
        "axiom new_axiom : False\n"
        "partial def loop : Nat → Nat := fun n => loop (n+1)\n"
        "theorem foo : 1 = 1 := rfl\n"
    )
    verdict, findings = compare(base, head)
    assert verdict == Verdict.INTRODUCED
    patterns = sorted(f.pattern for f in findings)
    assert patterns == ["axiom", "partial"]


def test_compare_finding_delta_is_net_increase() -> None:
    base = "axiom a : Nat\n"
    head = "axiom a : Nat\naxiom b : Nat\naxiom c : Nat\n"
    verdict, findings = compare(base, head)
    assert verdict == Verdict.INTRODUCED
    assert len(findings) == 1
    assert findings[0].delta == 2


# ---------------------------------------------------------------------------
# format_findings
# ---------------------------------------------------------------------------


def test_format_findings_empty() -> None:
    assert "No new" in format_findings([])


def test_format_findings_table_includes_pattern_and_delta() -> None:
    base = "theorem foo : 1 = 1 := rfl\n"
    head = "axiom bad : False\n" + base
    _verdict, findings = compare(base, head)
    out = format_findings(findings)
    assert "axiom" in out
    assert "+1" in out


# ---------------------------------------------------------------------------
# extract_axiom_names
# ---------------------------------------------------------------------------


def test_extract_names_empty() -> None:
    assert extract_axiom_names("") == []


def test_extract_names_simple() -> None:
    src = "axiom choice : Nonempty α → α\naxiom em : ∀ p, p ∨ ¬ p\n"
    assert extract_axiom_names(src) == ["choice", "em"]


def test_extract_names_with_dots_in_identifiers() -> None:
    # Lean identifiers can be dotted (`Foo.bar`); whitelist needs to
    # accept these.
    src = "axiom Quot.sound : ∀ {α} {r}, r a b → Quot.mk r a = Quot.mk r b\n"
    assert extract_axiom_names(src) == ["Quot.sound"]


def test_extract_names_preserves_duplicates_and_order() -> None:
    src = "axiom a : Nat\naxiom b : Nat\naxiom a : Nat\n"
    assert extract_axiom_names(src) == ["a", "b", "a"]


def test_extract_names_doesnt_match_keyword_in_identifier() -> None:
    src = "def axiomatic_foo : Nat := 42\n"
    assert extract_axiom_names(src) == []


# ---------------------------------------------------------------------------
# Whitelist policy
# ---------------------------------------------------------------------------


def test_whitelist_clean_when_all_axioms_in_allowed_set() -> None:
    base = ""
    head = (
        "axiom propext : ∀ {a b : Prop}, (a ↔ b) → a = b\n"
        "axiom Quot.sound : True\n"
    )
    cfg = AxiomHonestyConfig(
        policy=AxiomPolicy.WHITELIST,
        allowed_axioms=("propext", "Quot.sound", "Classical.choice"),
    )
    verdict, findings = compare(base, head, config=cfg)
    assert verdict == Verdict.CLEAN
    assert findings == []


def test_whitelist_flags_disallowed_axiom() -> None:
    base = ""
    head = "axiom bad : False\n"
    cfg = AxiomHonestyConfig(
        policy=AxiomPolicy.WHITELIST,
        allowed_axioms=("propext", "Quot.sound", "Classical.choice"),
    )
    verdict, findings = compare(base, head, config=cfg)
    assert verdict == Verdict.INTRODUCED
    assert len(findings) == 1
    assert findings[0].pattern == "axiom"
    assert findings[0].disallowed_names == ("bad",)


def test_whitelist_flags_even_axioms_already_in_base() -> None:
    # Distinguishes whitelist from net-zero: an axiom present in *both*
    # base and head but not on the whitelist still fails. The maintainer
    # may have introduced a non-whitelist axiom by mistake earlier; the
    # whitelist policy catches it on the next PR through the same file.
    base = "axiom legacy_bad : False\n"
    head = base
    cfg = AxiomHonestyConfig(
        policy=AxiomPolicy.WHITELIST,
        allowed_axioms=("propext",),
    )
    verdict, findings = compare(base, head, config=cfg)
    assert verdict == Verdict.INTRODUCED
    assert findings[0].disallowed_names == ("legacy_bad",)


def test_whitelist_deduplicates_names_in_report() -> None:
    base = ""
    head = "axiom bad : Nat\naxiom bad : Nat\naxiom other : Nat\n"
    cfg = AxiomHonestyConfig(
        policy=AxiomPolicy.WHITELIST, allowed_axioms=()
    )
    _verdict, findings = compare(base, head, config=cfg)
    assert findings[0].disallowed_names == ("bad", "other")


def test_whitelist_other_patterns_still_net_zero() -> None:
    # Under whitelist, `partial`/`unsafe`/etc. still use net-zero
    # comparison — whitelist scope is `axiom` only.
    base = "partial def f : Nat → Nat := fun n => f (n+1)\n"
    head = base  # same partial in both — not a violation
    cfg = AxiomHonestyConfig(
        policy=AxiomPolicy.WHITELIST, allowed_axioms=()
    )
    verdict, findings = compare(base, head, config=cfg)
    assert verdict == Verdict.CLEAN
    assert findings == []


def test_whitelist_combines_axiom_violation_with_net_zero_partial() -> None:
    base = "theorem foo : 1 = 1 := rfl\n"
    head = (
        "axiom bad : False\n"
        "partial def f : Nat → Nat := fun n => f (n+1)\n"
    )
    cfg = AxiomHonestyConfig(
        policy=AxiomPolicy.WHITELIST, allowed_axioms=()
    )
    verdict, findings = compare(base, head, config=cfg)
    assert verdict == Verdict.INTRODUCED
    patterns = sorted(f.pattern for f in findings)
    assert patterns == ["axiom", "partial"]


def test_whitelist_disallowed_names_rendered_in_findings() -> None:
    base = ""
    head = "axiom bad_name : False\n"
    cfg = AxiomHonestyConfig(
        policy=AxiomPolicy.WHITELIST, allowed_axioms=("propext",)
    )
    _verdict, findings = compare(base, head, config=cfg)
    out = format_findings(findings)
    assert "bad_name" in out
    assert "whitelist" in out.lower()


# ---------------------------------------------------------------------------
# Per-prover: isabelle (axiomatization/oracle) and rocq (Parameter/
# native_compute/Unset Universe Checking), net_zero policy.
# ---------------------------------------------------------------------------


def test_isabelle_count_patterns_uses_profile_trust_patterns() -> None:
    src = "axiomatization\n  foo :: nat\n\noracle bar = ...\n"
    counts = count_patterns(src, patterns=compile_patterns(ISABELLE.trust_patterns))
    assert counts["axiomatization"] == 1
    assert counts["oracle"] == 1


def test_isabelle_compare_flags_new_axiomatization() -> None:
    base = "lemma foo: \"a = a\" by simp\n"
    head = base + "axiomatization\n  bad :: nat\n"
    verdict, findings = compare(base, head, profile=ISABELLE)
    assert verdict == Verdict.INTRODUCED
    assert any(f.pattern == "axiomatization" for f in findings)


def test_isabelle_compare_flags_new_oracle() -> None:
    base = "lemma foo: \"a = a\" by simp\n"
    head = base + "oracle bad = ...\n"
    verdict, findings = compare(base, head, profile=ISABELLE)
    assert verdict == Verdict.INTRODUCED
    assert any(f.pattern == "oracle" for f in findings)


def test_isabelle_compare_clean_when_unchanged() -> None:
    src = "axiomatization\n  foo :: nat\n"
    verdict, findings = compare(src, src, profile=ISABELLE)
    assert verdict == Verdict.CLEAN
    assert findings == []


def test_rocq_compare_flags_new_parameter() -> None:
    base = "Theorem foo : True.\nProof. reflexivity. Qed.\n"
    head = base + "Parameter bad : nat.\n"
    verdict, findings = compare(base, head, profile=ROCQ)
    assert verdict == Verdict.INTRODUCED
    assert any(f.pattern == "axiom" for f in findings)


def test_rocq_compare_flags_new_native_compute() -> None:
    base = "Theorem foo : True.\nProof. reflexivity. Qed.\n"
    head = "Theorem foo : True.\nProof. native_compute. Qed.\n"
    verdict, findings = compare(base, head, profile=ROCQ)
    assert verdict == Verdict.INTRODUCED
    assert any(f.pattern == "native_compute" for f in findings)


def test_rocq_compare_flags_new_universe_checking() -> None:
    base = ""
    head = "Unset Universe Checking.\n"
    verdict, findings = compare(base, head, profile=ROCQ)
    assert verdict == Verdict.INTRODUCED
    assert any(f.pattern == "universe_checking" for f in findings)


def test_rocq_compare_clean_when_unchanged() -> None:
    src = "Parameter foo : nat.\n"
    verdict, findings = compare(src, src, profile=ROCQ)
    assert verdict == Verdict.CLEAN
    assert findings == []


# ---------------------------------------------------------------------------
# Whitelist fail-closed on profiles without an axiom-name extractor
# (P2 Task 3 review, finding 1): whitelist + rocq must not silently pass
# a new bare `Axiom bad : False.` — it should degrade to net-zero for the
# axiom-labeled pattern and say so in the report.
# ---------------------------------------------------------------------------


def test_rocq_whitelist_degrades_to_net_zero_for_new_axiom() -> None:
    base = "Theorem t : True.\nProof. exact I. Qed.\n"
    head = base + "Axiom bad_assumption : False.\n"
    whitelist_cfg = AxiomHonestyConfig(policy=AxiomPolicy.WHITELIST, allowed_axioms=())
    net_zero_cfg = AxiomHonestyConfig(policy=AxiomPolicy.NET_ZERO)

    verdict, findings = compare(base, head, config=whitelist_cfg, profile=ROCQ)
    net_verdict, net_findings = compare(base, head, config=net_zero_cfg, profile=ROCQ)

    # Whitelist mode must yield the same blocking verdict as net-zero
    # here — it must not silently pass just because name-matching isn't
    # implemented for rocq.
    assert verdict == net_verdict == Verdict.INTRODUCED
    assert [(f.pattern, f.base_count, f.head_count) for f in findings] == [
        (f.pattern, f.base_count, f.head_count) for f in net_findings
    ]
    # No disallowed-names whitelist violation was actually computed —
    # this is the net-zero fallback, not real name-matching.
    assert findings[0].disallowed_names == ()

    report = format_findings(findings)
    assert "whitelist" in report.lower()
    assert "not supported" in report.lower() or "unsupported" in report.lower()
    assert "rocq" in report.lower()


def test_rocq_whitelist_still_clean_when_no_new_axiom() -> None:
    # Degrading to net-zero must not introduce a false positive when
    # nothing actually changed.
    src = "Axiom legacy : False.\n"
    cfg = AxiomHonestyConfig(policy=AxiomPolicy.WHITELIST, allowed_axioms=())
    verdict, findings = compare(src, src, config=cfg, profile=ROCQ)
    assert verdict == Verdict.CLEAN
    assert findings == []


def test_isabelle_whitelist_still_catches_new_axiomatization() -> None:
    # Isabelle's pattern is labeled "axiomatization", not "axiom", so it
    # never reaches the whitelist branch at all — pin that it still uses
    # net-zero and catches the new declaration regardless.
    base = "lemma foo: \"a = a\" by simp\n"
    head = base + "axiomatization\n  bad :: bool\n"
    cfg = AxiomHonestyConfig(policy=AxiomPolicy.WHITELIST, allowed_axioms=())
    verdict, findings = compare(base, head, config=cfg, profile=ISABELLE)
    assert verdict == Verdict.INTRODUCED
    assert any(f.pattern == "axiomatization" for f in findings)


def test_lean4_whitelist_unaffected_by_fail_closed_change() -> None:
    # The oracle: lean4 whitelist behavior must remain byte-identical.
    base = ""
    head = "axiom bad : False\n"
    cfg = AxiomHonestyConfig(
        policy=AxiomPolicy.WHITELIST, allowed_axioms=("propext",)
    )
    verdict, findings = compare(base, head, config=cfg)
    assert verdict == Verdict.INTRODUCED
    assert len(findings) == 1
    assert findings[0].pattern == "axiom"
    assert findings[0].disallowed_names == ("bad",)
    # Real whitelist name-matching ran (lean4 is whitelist-capable) — no
    # "unsupported" note attached.
    assert findings[0].note is None


def test_compile_patterns_uses_multiline() -> None:
    # isabelle/rocq patterns use `^\s*` anchors; MULTILINE makes `^`
    # match at the start of every line, not just the start of the text.
    patterns = compile_patterns(ISABELLE.trust_patterns)
    src = "theory Scratch imports Main begin\naxiomatization\n  foo :: nat\n"
    assert patterns["axiomatization"].search(src) is not None
