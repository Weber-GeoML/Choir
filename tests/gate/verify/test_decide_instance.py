"""Tests for `gate.verify.decide_instance`."""

from __future__ import annotations

from gate.verify.decide_instance import (
    Verdict,
    compare,
    count_patterns,
    format_findings,
)

# ---------------------------------------------------------------------------
# count_patterns
# ---------------------------------------------------------------------------


def test_count_empty_file() -> None:
    assert count_patterns("") == {
        "decidable_instance": 0,
        "classical_choice": 0,
        "classical_decide": 0,
        "classical_by_contradiction": 0,
    }


def test_count_simple_decidable_instance() -> None:
    src = "instance : Decidable Foo := isFalse (by simp)\n"
    assert count_patterns(src)["decidable_instance"] == 1


def test_count_decidable_eq_instance() -> None:
    # DecidableEq should count — same trust concern as Decidable.
    src = "instance : DecidableEq MyType := fun a b => decEq a b\n"
    assert count_patterns(src)["decidable_instance"] == 1


def test_count_decidable_pred_instance() -> None:
    src = "instance : DecidablePred (fun n : Nat => n < 10) := inferInstance\n"
    assert count_patterns(src)["decidable_instance"] == 1


def test_count_named_decidable_instance() -> None:
    src = "instance fooDec : Decidable Foo := isFalse (by simp)\n"
    assert count_patterns(src)["decidable_instance"] == 1


def test_count_multiple_decidable_instances() -> None:
    src = (
        "instance : Decidable Foo := isFalse (by simp)\n"
        "instance : DecidableEq Bar := fun a b => decEq a b\n"
        "instance : DecidablePred P := inferInstance\n"
    )
    assert count_patterns(src)["decidable_instance"] == 3


def test_count_non_decidable_instance_not_matched() -> None:
    # Regular instance of an unrelated class.
    src = "instance : Monoid Nat := { ... }\n"
    assert count_patterns(src)["decidable_instance"] == 0


def test_count_classical_choice() -> None:
    src = "theorem foo : ∃ x, P x := Classical.choice ⟨a, ha⟩\n"
    assert count_patterns(src)["classical_choice"] == 1


def test_count_classical_decide() -> None:
    src = "theorem foo : P ∨ ¬P := Classical.decide _\n"
    assert count_patterns(src)["classical_decide"] == 1


def test_count_classical_prop_decidable() -> None:
    src = "noncomputable instance : Decidable P := Classical.propDecidable P\n"
    assert count_patterns(src)["classical_decide"] == 1


def test_count_classical_by_contradiction() -> None:
    src = "theorem foo : P := Classical.byContradiction fun h => ...\n"
    assert count_patterns(src)["classical_by_contradiction"] == 1


def test_count_classical_dec_eq() -> None:
    # Classical.decEq is the choice-backed Decidable equality.
    src = "theorem foo : a = b ∨ a ≠ b := Classical.decEq a b\n"
    assert count_patterns(src)["classical_decide"] == 1


def test_count_classical_namespace_not_matched_on_unrelated() -> None:
    # `Classical.foo` shouldn't count if it isn't one of the watched names.
    src = "open Classical in\ntheorem foo := bar\n"
    counts = count_patterns(src)
    assert counts["classical_choice"] == 0
    assert counts["classical_decide"] == 0


def test_decide_tactic_not_counted_as_classical() -> None:
    # `by decide` and `:= decide` use the kernel-checked Decidable
    # instance evaluation; they're safe iff the instance is correct.
    # This audit watches for new *instances* (and Classical usage),
    # not every `decide` invocation.
    src = "theorem foo : 2 + 2 = 4 := by decide\n"
    counts = count_patterns(src)
    assert counts["decidable_instance"] == 0
    assert counts["classical_decide"] == 0


# ---------------------------------------------------------------------------
# compare
# ---------------------------------------------------------------------------


def test_compare_clean_when_both_empty() -> None:
    verdict, findings = compare("", "")
    assert verdict == Verdict.CLEAN
    assert findings == []


def test_compare_clean_when_unchanged() -> None:
    src = "instance : Decidable Foo := isFalse (by simp)\n"
    verdict, findings = compare(src, src)
    assert verdict == Verdict.CLEAN
    assert findings == []


def test_compare_flags_new_decidable_instance() -> None:
    base = "theorem foo : 1 = 1 := rfl\n"
    head = base + "instance : Decidable Bar := isFalse (by simp)\n"
    verdict, findings = compare(base, head)
    assert verdict == Verdict.INTRODUCED
    assert any(f.pattern == "decidable_instance" for f in findings)


def test_compare_flags_new_classical_choice() -> None:
    base = "theorem foo : 1 = 1 := rfl\n"
    head = base + "theorem bar := Classical.choice ⟨a, ha⟩\n"
    verdict, findings = compare(base, head)
    assert verdict == Verdict.INTRODUCED
    assert findings[0].pattern == "classical_choice"
    assert findings[0].delta == 1


def test_compare_doesnt_flag_decrease() -> None:
    # Removing classical usage is not flagged — only net additions.
    base = "instance : Decidable A := isFalse ?\ninstance : Decidable B := isFalse ?\n"
    head = "instance : Decidable A := isFalse ?\n"
    verdict, findings = compare(base, head)
    assert verdict == Verdict.CLEAN
    assert findings == []


def test_compare_flags_multiple_patterns_together() -> None:
    base = "theorem foo : 1 = 1 := rfl\n"
    head = (
        "instance : Decidable Bar := isFalse (by simp)\n"
        "theorem baz := Classical.byContradiction fun h => ...\n"
        "theorem qux := Classical.choice ⟨a, ha⟩\n"
    )
    verdict, findings = compare(base, head)
    assert verdict == Verdict.INTRODUCED
    patterns = sorted(f.pattern for f in findings)
    assert patterns == [
        "classical_by_contradiction",
        "classical_choice",
        "decidable_instance",
    ]


def test_compare_finding_delta_is_net_increase() -> None:
    base = "instance : Decidable A := isFalse ?\n"
    head = (
        "instance : Decidable A := isFalse ?\n"
        "instance : Decidable B := isFalse ?\n"
        "instance : Decidable C := isFalse ?\n"
    )
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
    head = "instance : Decidable Bar := isFalse (by simp)\n" + base
    _verdict, findings = compare(base, head)
    out = format_findings(findings)
    assert "decidable_instance" in out
    assert "+1" in out


# ---------------------------------------------------------------------------
# Bug #2 regression: multi-line instance Decidable declarations
# ---------------------------------------------------------------------------


def test_count_multiline_instance_keyword_then_decidable() -> None:
    # Before the fix this returned 0 because the regex was single-line.
    src = "instance\n  : Decidable Foo := isFalse (by simp)\n"
    assert count_patterns(src)["decidable_instance"] == 1


def test_count_multiline_instance_with_binders() -> None:
    src = "instance fooDec\n  [DecidableEq α]\n  : Decidable (P α) := isFalse (by simp)\n"
    assert count_patterns(src)["decidable_instance"] == 1


def test_count_multiline_instance_with_priority() -> None:
    src = "instance (priority := high)\n  : Decidable Foo := isFalse (by simp)\n"
    assert count_patterns(src)["decidable_instance"] == 1


def test_attack_add_multiline_decidable_instance_is_caught() -> None:
    # The smoking-gun from the code review: previously CLEAN.
    base = "theorem foo : 1 = 1 := rfl\n"
    head = (
        "theorem foo : 1 = 1 := rfl\n"
        "instance\n  : Decidable Bar := isFalse (by simp)\n"
    )
    verdict, findings = compare(base, head)
    assert verdict == Verdict.INTRODUCED
    assert any(f.pattern == "decidable_instance" for f in findings)


def test_decidable_inside_proof_body_not_counted() -> None:
    # A reference to `Decidable` inside a tactic block of some other
    # declaration shouldn't count as a new instance — instances are
    # scoped to actual `instance ...` declarations.
    src = (
        "theorem foo : True := by\n"
        "  have h : Decidable True := isTrue trivial\n"
        "  trivial\n"
    )
    assert count_patterns(src)["decidable_instance"] == 0


def test_two_separate_multiline_instances() -> None:
    src = (
        "instance\n  : Decidable Foo := isFalse (by simp)\n"
        "\n"
        "instance\n  : DecidableEq Bar := fun a b => decEq a b\n"
    )
    assert count_patterns(src)["decidable_instance"] == 2


def test_instance_without_decidable_in_header_not_counted() -> None:
    # Multi-line instance of a non-Decidable class should not count.
    src = "instance fooMonoid\n  : Monoid Foo := { ... }\n"
    assert count_patterns(src)["decidable_instance"] == 0
