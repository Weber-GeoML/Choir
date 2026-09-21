"""Tests for `gate.verify.sorry_delta`."""

from __future__ import annotations

from gate.provers.isabelle import ISABELLE
from gate.provers.rocq import ROCQ
from gate.verify.sorry_delta import (
    Verdict,
    compare,
    compare_reduction,
    count_sorries,
    format_finding,
)

# ---------------------------------------------------------------------------
# count_sorries
# ---------------------------------------------------------------------------


def test_count_empty() -> None:
    assert count_sorries("") == []


def test_count_one() -> None:
    out = count_sorries("theorem t : True := by sorry\n", "T.lean")
    assert len(out) == 1
    assert out[0].decl == "t"


def test_count_ignores_comments() -> None:
    src = "-- sorry\n/- sorry -/\ntheorem t : True := trivial\n"
    assert count_sorries(src) == []


# ---------------------------------------------------------------------------
# compare
# ---------------------------------------------------------------------------


def test_closing_a_sorry_is_clean() -> None:
    base = "theorem t : 1 = 1 := by sorry\n"
    head = "theorem t : 1 = 1 := rfl\n"
    verdict, finding = compare(base, head)
    assert verdict == Verdict.CLEAN
    assert finding is None


def test_unchanged_sorry_count_is_clean() -> None:
    # Editing a file that already carries a sorry (progress elsewhere
    # in the file) passes as long as the count doesn't grow.
    base = "theorem a : True := by sorry\ndef b : Nat := 1\n"
    head = "theorem a : True := by sorry\ndef b : Nat := 2\n"
    verdict, _ = compare(base, head)
    assert verdict == Verdict.CLEAN


def test_new_sorry_flags() -> None:
    base = "theorem a : True := trivial\n"
    head = "theorem a : True := trivial\ntheorem b : False := by sorry\n"
    verdict, finding = compare(base, head, file_path="X.lean")
    assert verdict == Verdict.INTRODUCED
    assert finding is not None
    assert finding.base_count == 0
    assert finding.head_count == 1
    assert finding.delta == 1
    assert finding.head_sorries[0].decl == "b"


def test_new_file_with_sorry_flags() -> None:
    # Base content '' (file didn't exist) — any sorry in head is net-new.
    verdict, finding = compare("", "theorem t : True := by sorry\n")
    assert verdict == Verdict.INTRODUCED
    assert finding is not None
    assert finding.delta == 1


def test_swap_one_sorry_for_another_is_clean_by_count() -> None:
    # Count-based v0: closing one sorry while opening another nets to
    # zero and passes. Documented limitation — the statement-equiv and
    # orchestrator review layers see the actual diff.
    base = "theorem a : True := by sorry\ntheorem b : True := trivial\n"
    head = "theorem a : True := trivial\ntheorem b : True := by sorry\n"
    verdict, _ = compare(base, head)
    assert verdict == Verdict.CLEAN


def test_comment_mentioning_sorry_does_not_flag() -> None:
    base = "theorem t : 1 = 1 := by sorry\n"
    head = "-- closed the sorry below\ntheorem t : 1 = 1 := rfl\n"
    verdict, _ = compare(base, head)
    assert verdict == Verdict.CLEAN


# ---------------------------------------------------------------------------
# format_finding
# ---------------------------------------------------------------------------


def test_format_lists_decls_and_delta() -> None:
    _, finding = compare("", "theorem hard : False := by sorry\n")
    assert finding is not None
    out = format_finding(finding)
    assert "+1" in out
    assert "`hard`" in out


# ---------------------------------------------------------------------------
# Per-prover: isabelle (oops) and rocq (Admitted/admit)
# ---------------------------------------------------------------------------


def test_isabelle_count_sorries_counts_oops() -> None:
    src = "lemma foo:\n  \"a = a\"\n  oops\n"
    out = count_sorries(src, "F.thy", profile=ISABELLE)
    assert len(out) == 1


def test_isabelle_new_oops_flags_introduced() -> None:
    base = "lemma foo: \"a = a\" by simp\n"
    head = base + "lemma bar:\n  \"b = b\"\n  oops\n"
    verdict, finding = compare(base, head, file_path="F.thy", profile=ISABELLE)
    assert verdict == Verdict.INTRODUCED
    assert finding is not None
    assert finding.delta == 1


def test_isabelle_commented_oops_does_not_flag() -> None:
    base = "lemma foo: \"a = a\" by simp\n"
    head = "(* oops *)\n" + base
    verdict, _ = compare(base, head, profile=ISABELLE)
    assert verdict == Verdict.CLEAN


def test_rocq_count_sorries_counts_admitted_and_admit() -> None:
    src = (
        "Theorem foo : True.\nProof. admit. Qed.\n"
        "Lemma bar : True.\nAdmitted.\n"
    )
    out = count_sorries(src, "F.v", profile=ROCQ)
    assert len(out) == 2


def test_rocq_new_admitted_flags_introduced() -> None:
    base = "Theorem foo : True.\nProof. reflexivity. Qed.\n"
    head = base + "Lemma bar : True.\nAdmitted.\n"
    verdict, finding = compare(base, head, file_path="F.v", profile=ROCQ)
    assert verdict == Verdict.INTRODUCED
    assert finding is not None
    assert finding.delta == 1


def test_rocq_commented_admitted_does_not_flag() -> None:
    base = "Theorem foo : True.\nProof. reflexivity. Qed.\n"
    head = "(* Admitted. *)\n" + base
    verdict, _ = compare(base, head, profile=ROCQ)
    assert verdict == Verdict.CLEAN


# ---------------------------------------------------------------------------
# compare_reduction
# ---------------------------------------------------------------------------

# A published target: the orchestrator's committed placeholder.
BASE = "theorem parent : True := by\n  sorry\n"

# What a reduction looks like: the target proved, two obligations stated.
HEAD_REDUCED = (
    "theorem parent : True := And.intro childA childB\n\n"
    "theorem childA : True := by\n  sorry\n\n"
    "theorem childB : True := by\n  sorry\n"
)


def test_new_declarations_may_carry_placeholders() -> None:
    verdict, finding = compare_reduction(
        BASE, HEAD_REDUCED, target_decl="NS.parent", children=["childA", "childB"]
    )
    assert verdict is Verdict.CLEAN
    assert finding is None


def test_placeholder_on_the_target_is_introduced() -> None:
    head = (
        "theorem parent : True := by\n  sorry\n\n"
        "theorem childA : True := by\n  sorry\n"
    )
    verdict, finding = compare_reduction(
        BASE, head, target_decl="NS.parent", children=["childA"]
    )
    assert verdict is Verdict.INTRODUCED
    assert finding is not None
    assert [s.decl for s in finding.head_sorries] == ["parent"]


def test_target_is_refused_even_when_absent_from_base() -> None:
    # The target was never committed to base, so Rule B's base/children
    # exception has nothing to grant — only Rule A, which is
    # unconditional, refuses it.
    head = "theorem parent : True := by\n  sorry\n"
    verdict, finding = compare_reduction(
        "", head, target_decl="NS.parent", children=()
    )
    assert verdict is Verdict.INTRODUCED
    assert finding is not None
    assert [s.decl for s in finding.head_sorries] == ["parent"]


def test_citing_an_already_sorried_obligation_is_clean() -> None:
    # `childA` is sorried at base and stays exactly as sorried at head —
    # its count does not rise, so Rule B never examines it, matching
    # `ReductionChild`'s contract that a `decl` may name an obligation
    # already in the corpus, not only one the PR adds.
    base = "theorem childA : True := by\n  sorry\n"
    head = base + "theorem parent : True := childA\n"
    verdict, finding = compare_reduction(
        base, head, target_decl="NS.parent", children=["childA"]
    )
    assert verdict is Verdict.CLEAN
    assert finding is None


def test_placeholder_added_to_an_existing_declaration_is_introduced() -> None:
    base = "theorem parent : True := trivial\ntheorem other : True := trivial\n"
    head = (
        "theorem parent : True := other\n"
        "theorem other : True := by\n  sorry\n"
    )
    verdict, finding = compare_reduction(
        base, head, target_decl="NS.parent", children=()
    )
    assert verdict is Verdict.INTRODUCED
    assert finding is not None
    assert [s.decl for s in finding.head_sorries] == ["other"]


def test_a_placeholder_moved_onto_an_existing_declaration_is_introduced() -> None:
    # Base and head carry the same total (1 == 1): the target's own
    # placeholder closed, but the one that appeared instead sits on a
    # declaration the base corpus already had. A count-based early
    # return would call this clean; the reduction contract must not.
    base = "theorem parent : True := by\n  sorry\ntheorem other : True := trivial\n"
    head = "theorem parent : True := trivial\ntheorem other : True := by\n  sorry\n"
    verdict, finding = compare_reduction(
        base, head, target_decl="NS.parent", children=()
    )
    assert verdict is Verdict.INTRODUCED
    assert finding is not None
    assert [s.decl for s in finding.head_sorries] == ["other"]


def test_placeholder_with_no_enclosing_declaration_is_introduced() -> None:
    head = "sorry\ntheorem parent : True := trivial\n"
    verdict, finding = compare_reduction(
        "theorem parent : True := trivial\n",
        head,
        target_decl="NS.parent",
        children=(),
    )
    assert verdict is Verdict.INTRODUCED
    assert finding is not None


def test_the_ordinary_contract_refuses_what_a_reduction_permits() -> None:
    # The two contracts must differ on one identical input: without a
    # declared reduction the same diff still fails, which is what makes
    # the block load-bearing rather than decorative.
    reduction_verdict, _f = compare_reduction(
        BASE, HEAD_REDUCED, target_decl="NS.parent", children=["childA", "childB"]
    )
    ordinary_verdict, _g = compare(BASE, HEAD_REDUCED)
    assert reduction_verdict is Verdict.CLEAN
    assert ordinary_verdict is Verdict.INTRODUCED


def test_isabelle_new_declarations_may_carry_oops() -> None:
    base = 'lemma parent: "a = a" oops\n'
    head = (
        'lemma parent: "a = a" by simp\n'
        'lemma childA:\n  "b = b"\n  oops\n'
        'lemma childB:\n  "c = c"\n  oops\n'
    )
    verdict, finding = compare_reduction(
        base,
        head,
        file_path="F.thy",
        target_decl="parent",
        children=["childA", "childB"],
        profile=ISABELLE,
    )
    assert verdict is Verdict.CLEAN
    assert finding is None


def test_rocq_placeholder_on_the_target_is_introduced() -> None:
    base = "Theorem parent : True.\nAdmitted.\n"
    head = (
        "Theorem parent : True.\nAdmitted.\n"
        "Lemma childA : True.\nAdmitted.\n"
    )
    verdict, finding = compare_reduction(
        base,
        head,
        file_path="F.v",
        target_decl="parent",
        children=["childA"],
        profile=ROCQ,
    )
    assert verdict is Verdict.INTRODUCED
    assert finding is not None
    assert [s.decl for s in finding.head_sorries] == ["parent"]


# ---------------------------------------------------------------------------
# compare_reduction — adversarial cases
# ---------------------------------------------------------------------------


def test_target_written_qualified_in_a_new_file_is_introduced() -> None:
    # The declaration line spells the target with its own namespace
    # prefix, so the attributed name and `target_decl` are qualified
    # differently. `children` deliberately lists the target's own leaf:
    # if Rule A's leaf comparison were not what refuses this sorry, Rule
    # B would read it as a legitimately declared, base-absent child and
    # let it through instead.
    head = "theorem NS.parent : Big := by\n  sorry\n"
    verdict, finding = compare_reduction(
        "", head, target_decl="NS.parent", children=["NS.parent"]
    )
    assert verdict is Verdict.INTRODUCED
    assert finding is not None
    assert [s.decl for s in finding.head_sorries] == ["NS.parent"]


def test_trailing_dot_target_refuses_every_net_new_placeholder() -> None:
    # A malformed target (trailing dot) splits to an empty leaf. Even a
    # placeholder in a properly declared child must still be refused —
    # a degenerate target fails closed rather than matching nothing.
    base = "theorem parent : True := trivial\n"
    head = (
        "theorem parent : True := trivial\n"
        "theorem childA : True := by\n  sorry\n"
    )
    verdict, finding = compare_reduction(
        base, head, target_decl="Project.main_bound.", children=["childA"]
    )
    assert verdict is Verdict.INTRODUCED
    assert finding is not None
    assert [s.decl for s in finding.head_sorries] == ["childA"]


def test_a_falling_count_does_not_hide_an_unproved_target() -> None:
    # The target's own count does not rise (1 -> 1, unchanged) — a
    # delta-only rule would never examine it. Rule A is unconditional
    # precisely so a target this still sorried cannot hide behind a
    # different declaration's count falling elsewhere in the same file.
    base = (
        "theorem parent : True := by\n  sorry\n"
        "theorem helper : True := by\n  sorry\n"
    )
    head = (
        "theorem parent : True := by\n  sorry\n"
        "theorem helper : True := trivial\n"
    )
    verdict, finding = compare_reduction(
        base, head, target_decl="NS.parent", children=()
    )
    assert verdict is Verdict.INTRODUCED
    assert finding is not None
    assert [s.decl for s in finding.head_sorries] == ["parent"]


def test_a_requalified_existing_declaration_is_introduced() -> None:
    # Base declares `helper` inside a namespace, attributed unqualified
    # ("helper"). Head repeats it outside the namespace, written with an
    # explicit prefix ("NS.helper") and a sorry added. `children`
    # deliberately lists the same leaf: if Rule B's base comparison were
    # not leaf-to-leaf, `NS.helper` would read as absent from base and,
    # since its leaf is declared, get permitted instead of refused.
    base = "namespace NS\ntheorem helper : True := trivial\nend NS\n"
    head = "theorem NS.helper : True := by\n  sorry\n"
    verdict, finding = compare_reduction(
        base, head, target_decl="NS.parent", children=["helper"]
    )
    assert verdict is Verdict.INTRODUCED
    assert finding is not None
    assert [s.decl for s in finding.head_sorries] == ["NS.helper"]


def test_a_rise_onto_a_pre_existing_shared_leaf_is_not_laundered() -> None:
    # `A.f` and `B.f` are two declarations that share a last segment.
    # The placeholder moves off `A.f` onto `B.f` — a declaration base
    # already had and `children` never named — so the total under that
    # shared segment holds at one. Counting per name as written is what
    # sees the rise; a leaf-keyed count reads this as no rise at all and
    # Rule B, which examines only what rose, never looks at it.
    base = "theorem A.f : True := by\n  sorry\ntheorem B.f : True := trivial\n"
    head = "theorem A.f : True := trivial\ntheorem B.f : True := by\n  sorry\n"
    verdict, finding = compare_reduction(
        base, head, target_decl="NS.parent", children=()
    )
    assert verdict is Verdict.INTRODUCED
    assert finding is not None
    assert [s.decl for s in finding.head_sorries] == ["B.f"]


def test_a_rise_onto_a_new_declaration_sharing_a_leaf_is_not_laundered() -> None:
    # The same laundering with the receiving declaration brand new and
    # still absent from `children`: `B.f` is not an obligation this
    # submission declared, so it is refused whether or not base had it.
    base = "theorem A.f : True := by\n  sorry\n"
    head = "theorem A.f : True := trivial\ntheorem B.f : True := by\n  sorry\n"
    verdict, finding = compare_reduction(
        base, head, target_decl="NS.parent", children=()
    )
    assert verdict is Verdict.INTRODUCED
    assert finding is not None
    assert [s.decl for s in finding.head_sorries] == ["B.f"]


def test_a_rise_hidden_by_a_namespace_block_spelling_is_refused() -> None:
    # The same laundering in Lean's idiomatic spelling: two `theorem f`s
    # in sibling `namespace` blocks, both attributed to the surface name
    # `f`, with the placeholder moving from one block to the other. Only
    # a scope-qualified count key tells `A.f` from `B.f` and sees the
    # rise; on the surface name the pooled count holds at one.
    base = (
        "namespace A\ntheorem f : True := by\n  sorry\nend A\n"
        "namespace B\ntheorem f : True := trivial\nend B\n"
        "theorem parent : True := by\n  sorry\n"
    )
    head = (
        "namespace A\ntheorem f : True := trivial\nend A\n"
        "namespace B\ntheorem f : True := by\n  sorry\nend B\n"
        "theorem parent : True := A.f\n"
    )
    verdict, finding = compare_reduction(
        base, head, target_decl="NS.parent", children=()
    )
    assert verdict is Verdict.INTRODUCED
    assert finding is not None
    assert [s.line for s in finding.head_sorries] == [6]


def test_rocq_a_rise_hidden_by_a_module_block_spelling_is_refused() -> None:
    # The same shape in rocq, where `Module A` qualifies what is inside
    # it exactly as a Lean namespace does.
    base = (
        "Module A.\nTheorem f : True.\nAdmitted.\nEnd A.\n"
        "Module B.\nTheorem f : True.\nProof. trivial. Qed.\nEnd B.\n"
    )
    head = (
        "Module A.\nTheorem f : True.\nProof. trivial. Qed.\nEnd A.\n"
        "Module B.\nTheorem f : True.\nAdmitted.\nEnd B.\n"
    )
    verdict, finding = compare_reduction(
        base,
        head,
        file_path="F.v",
        target_decl="parent",
        children=(),
        profile=ROCQ,
    )
    assert verdict is Verdict.INTRODUCED
    assert finding is not None
    assert [s.line for s in finding.head_sorries] == [7]


def test_requalifying_a_sorried_declaration_is_not_a_rise() -> None:
    # One declaration, one placeholder, two spellings: `theorem
    # NS.helper` at base, the same theorem inside a `namespace NS` block
    # at head. Qualifying both sides gives it one key, so this ordinary
    # tidy-up is the non-rise it is and Rule B never examines it.
    base = "theorem NS.helper : True := by\n  sorry\n"
    head = "namespace NS\ntheorem helper : True := by\n  sorry\nend NS\n"
    verdict, finding = compare_reduction(
        base, head, target_decl="NS.parent", children=["helper"]
    )
    assert verdict is Verdict.CLEAN
    assert finding is None


def test_a_child_leaf_matching_two_head_declarations_admits_neither() -> None:
    # `children` names one obligation, `f`, and head introduces two
    # declarations whose last segment is `f`. A leaf that could be
    # either declares neither: an obligation the submission did not
    # state must not ride in on a name collision.
    base = "theorem parent : True := by\n  sorry\n"
    head = (
        "theorem parent : True := And.intro A.f B.f\n"
        "namespace A\ntheorem f : True := by\n  sorry\nend A\n"
        "namespace B\ntheorem f : True := by\n  sorry\nend B\n"
    )
    verdict, finding = compare_reduction(
        base, head, target_decl="NS.parent", children=("f",)
    )
    assert verdict is Verdict.INTRODUCED
    assert finding is not None
    assert [s.line for s in finding.head_sorries] == [4, 8]


def test_an_unambiguous_child_leaf_still_admits_its_placeholder() -> None:
    # The control for the rule above: the same declared leaf, with only
    # one head declaration carrying it, is still an admitted obligation.
    base = "theorem parent : True := by\n  sorry\n"
    head = (
        "theorem parent : True := A.f\n"
        "namespace A\ntheorem f : True := by\n  sorry\nend A\n"
    )
    verdict, finding = compare_reduction(
        base, head, target_decl="NS.parent", children=("f",)
    )
    assert verdict is Verdict.CLEAN
    assert finding is None


def test_the_same_move_between_distinct_leaves_is_introduced() -> None:
    # The control for the two above: an identical shape whose last
    # segments differ, so the rise is visible under either counting key.
    # It pins that the two are testing the shared-leaf pooling and not
    # the move itself.
    base = "theorem A.g : True := by\n  sorry\ntheorem B.h : True := trivial\n"
    head = "theorem A.g : True := trivial\ntheorem B.h : True := by\n  sorry\n"
    verdict, finding = compare_reduction(
        base, head, target_decl="NS.parent", children=()
    )
    assert verdict is Verdict.INTRODUCED
    assert finding is not None
    assert [s.decl for s in finding.head_sorries] == ["B.h"]


def test_a_placeholder_absent_from_children_is_introduced() -> None:
    # `extra` is genuinely new (absent from base) and is not the
    # target, but the submission never declared it as an obligation —
    # `children` does not list it — so it is still refused.
    base = "theorem parent : True := trivial\n"
    head = (
        "theorem parent : True := trivial\n"
        "theorem extra : True := by\n  sorry\n"
    )
    verdict, finding = compare_reduction(
        base, head, target_decl="NS.parent", children=["childA"]
    )
    assert verdict is Verdict.INTRODUCED
    assert finding is not None
    assert [s.decl for s in finding.head_sorries] == ["extra"]


def test_rocq_a_scope_closer_split_over_two_lines_refuses_the_shared_key() -> None:
    # `End A.` is one Rocq command however it is spread over lines, but
    # the qualifier reads lines, so `End` / `A.` leaves module `A` open
    # and the top-level `f` after it keys as `A.f` — the same key the
    # `f` inside the module already has. Pooled, the placeholder moving
    # from the one onto the other holds the count at one and Rule B
    # never looks. Two head declarations sharing a key refuses instead.
    base = (
        "Theorem main : True.\nProof. exact I. Qed.\n"
        "Module A.\nTheorem f : True.\nAdmitted.\nEnd A.\n"
        "Theorem f : True.\nProof. exact I. Qed.\n"
    )
    head = (
        "Theorem main : True.\nProof. exact I. Qed.\n"
        "Module A.\nTheorem f : True.\nProof. exact I. Qed.\n"
        "End\nA.\n"
        "Theorem f : True.\nAdmitted.\n"
    )
    verdict, finding = compare_reduction(
        base,
        head,
        file_path="F.v",
        target_decl="main",
        children=("f",),
        profile=ROCQ,
    )
    assert verdict is Verdict.INTRODUCED
    assert finding is not None
    assert [s.line for s in finding.head_sorries] == [9]


def test_a_namespace_opener_inside_a_string_literal_refuses_the_shared_key() -> None:
    # The scan reads a string's content as ordinary text, so a
    # `namespace A` line written inside one opens a scope that Lean
    # itself never opens. The top-level `f` below it then keys as `A.f`,
    # colliding with the real `A.f`, and the same pooled count hides the
    # same move.
    base = (
        "theorem main : True := trivial\n"
        "namespace A\ntheorem f : True := by\n  sorry\nend A\n"
        "theorem f : True := trivial\n"
    )
    head = (
        "theorem main : True := trivial\n"
        "namespace A\ntheorem f : True := trivial\nend A\n"
        'def s : String := "\nnamespace A\n"\n'
        "theorem f : True := by\n  sorry\n"
    )
    verdict, finding = compare_reduction(
        base, head, target_decl="main", children=("f",)
    )
    assert verdict is Verdict.INTRODUCED
    assert finding is not None
    assert [s.line for s in finding.head_sorries] == [9]


def test_a_scope_closer_sharing_a_declaration_line_refuses_the_shared_key() -> None:
    # `theorem g : True := trivial end A` is two Lean commands on one
    # line and compiles. The line matches the declaration pattern, not
    # the end-of-scope pattern, so namespace `A` stays open in the
    # qualifier and the `f` after it collides with the `A.f` above.
    base = (
        "theorem main : True := trivial\n"
        "namespace A\ntheorem f : True := by\n  sorry\nend A\n"
        "theorem f : True := trivial\n"
    )
    head = (
        "theorem main : True := trivial\n"
        "namespace A\ntheorem f : True := trivial\n"
        "theorem g : True := trivial end A\n"
        "theorem f : True := by\n  sorry\n"
    )
    verdict, finding = compare_reduction(
        base, head, target_decl="main", children=("f",)
    )
    assert verdict is Verdict.INTRODUCED
    assert finding is not None
    assert [s.line for s in finding.head_sorries] == [6]
