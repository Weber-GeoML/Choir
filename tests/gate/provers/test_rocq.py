"""Tests for `gate.provers.rocq` — the rocq profile and its statement
extractor (design note 12 §2.2, §3.3).
"""

from __future__ import annotations

from gate.provers.base import CommentSyntax
from gate.provers.rocq import (
    ROCQ,
    extract_rocq_statement,
    qualify_rocq_decl_names,
)
from gate.verify.statement_equiv import Verdict, compare

# Authentic Rocq snippet (design note 12 §2.2 / task-2 brief), verbatim.
ROCQ_SRC = """
Require Import Arith.

Theorem add_comm_ex : forall a b : nat, a + b = b + a.
Proof. intros. apply Nat.add_comm. Qed.

Lemma le_trans_ex : forall x y z : nat, x <= y -> y <= z -> x <= z.
Proof. intros. eapply Nat.le_trans; eauto. Qed.
"""

# A theorem with a name that shares a prefix with `add_comm_ex`, so the
# extractor's word-boundary handling is exercised both ways.
ROCQ_SIMILAR_NAME = """
Theorem add_comm_ex_alt : forall a b : nat, a + b = b + a.
Proof. intros. apply Nat.add_comm. Qed.
"""

# A statement whose body itself contains qualified names (dots not
# followed by whitespace) — proves the sentence-end rule doesn't cut
# the statement short at `Nat.le` / `Nat.add`.
ROCQ_QUALIFIED = """
Lemma uses_qualified_ex : Nat.le 1 2 /\\ Nat.add 1 1 = 2.
Proof. split; [apply Nat.le_succ_diag_r | reflexivity]. Qed.
"""


# ---------------------------------------------------------------------------
# Declarative fields (note 12 §2.2/§3.3, verbatim)
# ---------------------------------------------------------------------------


def test_rocq_declarative_fields() -> None:
    assert ROCQ.name == "rocq"
    assert ROCQ.file_extensions == (".v",)
    assert ROCQ.comment_syntax == CommentSyntax(
        line=None, block_open="(*", block_close="*)", nested=True
    )
    # Round 5 (F2) re-derived this whole tuple from Rocq's own generated
    # grammar (`doc/tools/docgram/fullGrammar`) rather than the manual's
    # prose, grouped here by the grammar token each entry comes from.
    # `Property` was the defect that prompted it: a `thm_token`
    # alternative missing from enumeration entirely, so `Property p :
    # 1 = 1.` produced zero spans and its statement was rewritable.
    # `gate/provers/rocq.py` carries the productions verbatim and the
    # list of commands deliberately left out.
    assert ROCQ.decl_keywords == (
        # thm_token
        "Theorem",
        "Lemma",
        "Corollary",
        "Proposition",
        "Fact",
        "Remark",
        "Property",
        # def_token
        "Example",
        "Definition",
        "SubClass",
        # gallina: Let / Fixpoint / CoFixpoint, funind: Function
        "Let",
        "Fixpoint",
        "CoFixpoint",
        "Function",
        # gallina_ext
        "Instance",
        # inductive_token, finite_token — `Variant`/`Structure` were
        # added by the fix round (whole-slice review I3); `CoInductive`
        # by round 5, which found `Inductive` wired without its
        # `inductive_token` sibling.
        "Inductive",
        "CoInductive",
        "Variant",
        "Record",
        "Structure",
        "Class",
        # assumption_token / assumptions_token
        "Axiom",
        "Axioms",
        "Parameter",
        "Parameters",
        "Conjecture",
        "Conjectures",
        "Hypothesis",
        "Hypotheses",
        "Variable",
        "Variables",
        # gallina: rewrite-rule symbols (Rocq >= 9.0), kernel primitives
        "Symbol",
        "Symbols",
        "Primitive",
    )
    assert ROCQ.placeholder_tokens == ("Admitted", "admit", "Abort")
    assert ROCQ.build_command == ("dune", "build")
    assert ROCQ.toolchain_file is None
    assert ROCQ.protected_files == ("_CoqProject", "dune-project", "*.opam")
    assert ROCQ.extra_audits == ()
    assert ROCQ.search_tooling_note is False


def test_rocq_statement_keywords_equal_decl_keywords() -> None:
    # Unlike lean4, every rocq decl kind has a sentence
    # `extract_rocq_statement` can resolve (design note 12 §3.3).
    assert ROCQ.statement_keywords == ROCQ.decl_keywords


def test_rocq_trust_patterns() -> None:
    assert ROCQ.trust_patterns == (
        ("axiom", r"^\s*(?:Axiom|Axioms|Parameter|Parameters|Conjecture)\b"),
        ("admitted", r"^\s*Admitted\."),
        ("native_compute", r"\bnative_compute\b"),
        ("vm_compute", r"\bvm_compute\b"),
        ("universe_checking", r"^\s*Unset\s+Universe\s+Checking\b"),
        ("ml_module", r"^\s*Declare\s+ML\s+Module\b"),
    )


def test_rocq_one_probe_per_decl_is_true() -> None:
    # `Print Assumptions` doesn't echo the queried name in its own
    # output, so rocq probes one decl at a time — see
    # tests/gate/provers/test_trust.py for the real trust-report hooks
    # (design note 12 §4).
    assert ROCQ.one_probe_per_decl is True


def test_rocq_qualify_decl_names_is_a_scope_path_key() -> None:
    # Statement-immutability hardening round 4 (F2) supersedes task 4's
    # `is None` pin. Task 4 declined a qualifier because a *correct*
    # one needs nested modules, functors, `Module Type` and
    # `Import`/`Export`/`Include`, none of it checkable against a Rocq
    # toolchain (still none installed here). That reasoning was right
    # about name resolution and answers a question this hook is not
    # asked: the key only has to match base declarations against head
    # declarations within ONE file, so stable + structure-derived +
    # computed identically on both sides is the whole requirement. An
    # enclosing-`Module` path is that. See
    # `gate.provers.decl_syntax.qualify_by_scope` — it is a
    # disambiguation key, NOT name resolution.
    assert ROCQ.qualify_decl_names is qualify_rocq_decl_names


def test_rocq_scope_path_qualifies_sibling_modules() -> None:
    src = (
        "Module A.\n"
        "Theorem c : 1 = 1.\n"
        "End A.\n"
        "Module B.\n"
        "Theorem c : 2 = 2.\n"
        "End B.\n"
    )
    assert qualify_rocq_decl_names(src) == {2: "A.c", 5: "B.c"}


def test_rocq_scope_path_nests_and_pops() -> None:
    src = (
        "Module A.\n"
        "Module B.\n"
        "Theorem c : 1 = 1.\n"
        "End B.\n"
        "Theorem d : 2 = 2.\n"
        "End A.\n"
    )
    assert qualify_rocq_decl_names(src) == {3: "A.B.c", 5: "A.d"}


def test_rocq_section_is_tracked_but_does_not_qualify() -> None:
    # A Rocq `Section` does not qualify the declarations inside it, so
    # it contributes nothing to the path — but it must still be tracked,
    # or its `End S.` would pop the enclosing `Module A`. Both halves
    # are asserted here: `c` is `A.c` (not `A.S.c`), and `d`, after the
    # section closed, is still `A.d` rather than having lost `A`.
    src = (
        "Module A.\n"
        "Section S.\n"
        "Theorem c : 1 = 1.\n"
        "End S.\n"
        "Theorem d : 2 = 2.\n"
        "End A.\n"
    )
    assert qualify_rocq_decl_names(src) == {3: "A.c", 5: "A.d"}


def test_rocq_module_definition_is_not_a_scope_opener() -> None:
    # `Module A := B.` defines a module rather than opening a block, so
    # it has no `End` — treating it as an opener would leave the stack
    # one deep for the rest of the file and prefix every following
    # declaration with a scope that never closes.
    src = "Module A := B.\nTheorem c : 1 = 1.\n"
    assert qualify_rocq_decl_names(src) == {2: "c"}


def test_rocq_module_type_and_functor_headers_open_scopes() -> None:
    src = "Module Type T.\nParameter p : nat.\nEnd T.\n"
    assert qualify_rocq_decl_names(src) == {2: "T.p"}
    functor = "Module F (X : S).\nTheorem c : 1 = 1.\nEnd F.\n"
    assert qualify_rocq_decl_names(functor) == {2: "F.c"}


# ---------------------------------------------------------------------------
# extract_rocq_statement
# ---------------------------------------------------------------------------


def test_extract_theorem_stops_at_sentence_dot() -> None:
    out = extract_rocq_statement(ROCQ_SRC, "add_comm_ex")
    assert out == "Theorem add_comm_ex : forall a b : nat, a + b = b + a."


def test_extract_lemma_stops_at_sentence_dot() -> None:
    out = extract_rocq_statement(ROCQ_SRC, "le_trans_ex")
    assert (
        out
        == "Lemma le_trans_ex : forall x y z : nat, x <= y -> y <= z -> x <= z."
    )


def test_extract_returns_none_when_decl_missing() -> None:
    assert extract_rocq_statement(ROCQ_SRC, "no_such_theorem") is None


def test_extract_does_not_match_similarly_named_decl() -> None:
    # `add_comm_ex` must not match `add_comm_ex_alt` (word-boundary).
    assert extract_rocq_statement(ROCQ_SIMILAR_NAME, "add_comm_ex") is None


def test_extract_matches_the_similarly_named_decl_itself() -> None:
    out = extract_rocq_statement(ROCQ_SIMILAR_NAME, "add_comm_ex_alt")
    assert out == "Theorem add_comm_ex_alt : forall a b : nat, a + b = b + a."


def test_extract_qualified_name_dot_does_not_end_the_sentence() -> None:
    # `Nat.le` / `Nat.add`'s dots are followed by identifier characters,
    # not whitespace, so they must not truncate the statement early.
    out = extract_rocq_statement(ROCQ_QUALIFIED, "uses_qualified_ex")
    assert out == "Lemma uses_qualified_ex : Nat.le 1 2 /\\ Nat.add 1 1 = 2."


def test_profile_extract_statement_hook_is_wired_to_the_module_function() -> None:
    assert ROCQ.extract_statement is extract_rocq_statement
    assert (
        ROCQ.extract_statement(ROCQ_SRC, "add_comm_ex")
        == "Theorem add_comm_ex : forall a b : nat, a + b = b + a."
    )


def test_rocq_extract_statement_resolves_modifier_prefixed_declarations() -> None:
    """Enumeration went prefix-aware; the extractor had to follow.

    Task 1 populated `ROCQ.decl_modifiers`, so `decl_line_regex` finds
    `Local Definition foo` as a declaration. An extractor still assuming
    the keyword is the line's first token then cannot resolve the very
    declaration enumeration just handed it — and in
    `statement_immutability` that asymmetry surfaces as `UNDETERMINED`
    (a declaration whose statement could not be read), not as a miss.
    lean4 and isabelle got this splice in the same task; rocq was
    reported as a follow-up rather than expanded into silently.
    """
    cases = [
        ("Local Definition foo : nat := 3.", "foo"),
        ("Global Instance bar : Eq nat := eq.", "bar"),
        ("#[global] Definition baz : nat := 1.", "baz"),
    ]
    for source, name in cases:
        extracted = ROCQ.extract_statement(source, name)
        assert extracted is not None, f"{name} unresolved in {source!r}"
        assert name in extracted

    # Unprefixed declarations keep resolving exactly as before.
    plain = ROCQ.extract_statement("Theorem plain : 1 = 1. Proof. auto. Qed.", "plain")
    assert plain == "Theorem plain : 1 = 1."


def test_rocq_control_flags_map_one_to_one_onto_the_grammar_token() -> None:
    """`ProverProfile.decl_prefix_flags` pinned against `control_flag`.

    Transcribed from Rocq's own machine-generated grammar,
    `doc/tools/docgram/fullGrammar` (rocq-prover/rocq at `master`):

        control_flag: [
        | "Time"
        | "Instructions"
        | "Profile" OPT STRING
        | "Redirect" ne_string
        | "Timeout" natural
        | "AllocLimit" natural [ "Mw" | "kw" ]
        | "Fail"
        | "Succeed"
        ]

    Eight alternatives, eight fragments, in grammar order — so a
    ninth alternative appearing upstream trips this test rather than
    quietly becoming a fail-open. The behavioural coverage is in
    `tests/gate/verify/test_statement_immutability_promotion_gate.py`
    (`test_round6_f1_*`); this test pins the mapping.
    """
    assert len(ROCQ.decl_prefix_flags) == 8
    keywords = [
        fragment.split("(", 1)[0].split("\\", 1)[0]
        for fragment in ROCQ.decl_prefix_flags
    ]
    assert keywords == [
        "Time",
        "Instructions",
        "Profile",
        "Redirect",
        "Timeout",
        "AllocLimit",
        "Fail",
        "Succeed",
    ]
    # No fragment may fall back on `\S+`: an argument with no
    # terminator is the over-permissive prefix that invents span
    # boundaries on non-declaration lines.
    assert all(r"\S+" not in fragment for fragment in ROCQ.decl_prefix_flags)


def test_rocq_control_flags_are_spliced_into_the_statement_extractor() -> None:
    """Enumeration and extraction must agree, or every control-flagged
    declaration reports `UNDETERMINED` — which passes, so the fail-open
    would survive in a new spelling."""
    assert (
        ROCQ.extract_statement("Time Definition foo : nat := 5.\n", "foo")
        == "Time Definition foo : nat := 5."
    )


# ---------------------------------------------------------------------------
# Rocq does NOT share isabelle's header over-capture (round 7, F1).
#
# The isabelle extractor scans forward until a proof stop token or a
# blank line, so a proof-less command (`definition`, `consts`,
# `typedecl`, …) followed by another command with no blank line ran
# straight into it and false-blocked a declaration nobody touched. Rocq
# was checked for the same shape rather than assumed clear: every Rocq
# command ends at a sentence-terminating `.`, which
# `_scan_to_sentence_end` stops at unconditionally — there is no
# "reached no terminator" path for it to fall through. Pinned here so
# a later change that made the scan conditional would be caught.
# ---------------------------------------------------------------------------

_ROCQ_ADJACENT = """\
Definition f := 1.
Axiom ax : True.
Parameter p : nat.
Inductive t := C | D.
Theorem a : True.
Proof. exact I. Qed.
"""


def test_body_less_rocq_declarations_stop_at_their_own_sentence_end() -> None:
    expected = {
        "f": "Definition f := 1.",
        "ax": "Axiom ax : True.",
        "p": "Parameter p : nat.",
        "t": "Inductive t := C | D.",
        "a": "Theorem a : True.",
    }
    for name, sentence in expected.items():
        assert extract_rocq_statement(_ROCQ_ADJACENT, name) == sentence, name


def test_editing_a_neighbour_does_not_change_a_rocq_targets_statement() -> None:
    head = _ROCQ_ADJACENT.replace("Axiom ax : True.", "Axiom ax : 1 = 1.")
    assert head != _ROCQ_ADJACENT
    verdict, message = compare(_ROCQ_ADJACENT, head, "f", profile=ROCQ)
    assert verdict is Verdict.EQUIVALENT, message
    assert compare(_ROCQ_ADJACENT, head, "ax", profile=ROCQ)[0] is Verdict.CHANGED
