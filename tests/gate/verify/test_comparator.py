"""Tests for `gate.verify.comparator` — pure logic (design note 14 §4–6)."""

from __future__ import annotations

import json

import pytest

from gate.verify.comparator import (
    CHALLENGE_PREFIX,
    COMPARATOR_MIN_TOOLCHAIN,
    CORE_PERMITTED_AXIOMS,
    RESERVED_LIB_NAMES,
    SORRY_AXIOM,
    ComparatorConfigError,
    Outcome,
    base_module_set,
    challenge_root,
    challenge_side_build_error,
    choirbase_root,
    classify_output,
    comparator_config_json,
    declaration_signature,
    declared_names,
    imported_modules,
    lean_lib_names,
    module_of_path,
    parse_toolchain_version,
    permitted_axioms,
    private_decl_names,
    private_names_in_statement,
    rewrite_imports,
    solution_root,
    suffix_divergent_instances,
    toolchain_supported,
    workspace_lakefile,
)
from gate.verify.config import (
    AxiomHonestyConfig,
    AxiomPolicy,
    SorryDeltaConfig,
    SorryPolicy,
    VerifyConfig,
)

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------


def test_constants() -> None:
    assert CORE_PERMITTED_AXIOMS == ("propext", "Quot.sound", "Classical.choice")
    assert SORRY_AXIOM == "sorryAx"
    assert COMPARATOR_MIN_TOOLCHAIN == (4, 27)
    assert CHALLENGE_PREFIX == "ChoirBase"
    assert sorted(RESERVED_LIB_NAMES) == ["Challenge", "ChoirBase", "Solution"]


# ---------------------------------------------------------------------------
# toolchain floor
# ---------------------------------------------------------------------------


def test_parse_toolchain_version_reads_every_pin_spelling() -> None:
    assert parse_toolchain_version("leanprover/lean4:v4.31.0") == (4, 31)
    assert parse_toolchain_version("leanprover/lean4:v4.33.0-rc1") == (4, 33)
    assert parse_toolchain_version("leanprover/lean4:v4.14.0\n") == (4, 14)
    assert parse_toolchain_version("4.31.0") == (4, 31)
    assert parse_toolchain_version("nightly-2026-01-01") is None
    assert parse_toolchain_version("") is None


def test_toolchain_supported_reads_the_floor() -> None:
    assert toolchain_supported("leanprover/lean4:v4.27.0") is True
    assert toolchain_supported("leanprover/lean4:v4.31.0") is True
    assert toolchain_supported("leanprover/lean4:v5.0.0") is True
    assert toolchain_supported("leanprover/lean4:v4.14.0") is False
    # Unparseable is neither supported nor refused — the caller decides.
    assert toolchain_supported("nightly-2026-01-01") is None


# ---------------------------------------------------------------------------
# module names
# ---------------------------------------------------------------------------


def test_module_of_path_maps_a_source_path_to_its_module() -> None:
    assert module_of_path("SampleProject/Basic.lean") == "SampleProject.Basic"
    assert module_of_path("SampleProject.lean") == "SampleProject"


def test_base_module_set() -> None:
    mods = base_module_set(["Proj.lean", "Proj/Basic.lean", "Proj/Deep/Lemma.lean"])
    assert mods == frozenset({"Proj", "Proj.Basic", "Proj.Deep.Lemma"})


# ---------------------------------------------------------------------------
# import rewriting
# ---------------------------------------------------------------------------

MODS = frozenset({"Proj", "Proj.Basic"})


def test_rewrite_prefixes_internal_import() -> None:
    assert (
        rewrite_imports("import Proj.Basic\n", MODS)
        == f"import {CHALLENGE_PREFIX}.Proj.Basic\n"
    )


def test_rewrite_leaves_external_imports() -> None:
    src = "import Mathlib.Tactic\nimport Init\n"
    assert rewrite_imports(src, MODS) == src


def test_rewrite_only_whole_module_match() -> None:
    # `Projection` shares a prefix with `Proj` but is not an internal module.
    src = "import Projection\n"
    assert rewrite_imports(src, MODS) == src


def test_rewrite_preserves_non_import_lines() -> None:
    src = "import Proj\n\ntheorem t : True := trivial\n-- import Proj in a comment\n"
    out = rewrite_imports(src, MODS)
    assert f"import {CHALLENGE_PREFIX}.Proj\n" in out
    assert "theorem t : True := trivial" in out
    # The comment line does not start with `import`, so it is untouched.
    assert "-- import Proj in a comment" in out


def test_rewrite_preserves_leading_whitespace_and_comment() -> None:
    src = "  import Proj.Basic -- keep me\n"
    assert (
        rewrite_imports(src, MODS)
        == f"  import {CHALLENGE_PREFIX}.Proj.Basic -- keep me\n"
    )


# ---------------------------------------------------------------------------
# lakefile handling
# ---------------------------------------------------------------------------

SAMPLE_LAKEFILE = 'name = "proj"\nversion = "0.1.0"\n\n[[lean_lib]]\nname = "Proj"\n'


def test_lean_lib_names() -> None:
    assert lean_lib_names(SAMPLE_LAKEFILE) == ["Proj"]


def test_lean_lib_names_bad_toml() -> None:
    with pytest.raises(ComparatorConfigError):
        lean_lib_names("name = [unclosed")


def test_workspace_lakefile_appends_three_libs() -> None:
    out = workspace_lakefile(SAMPLE_LAKEFILE)
    assert out.startswith(SAMPLE_LAKEFILE)
    for lib in ("Challenge", "Solution", "ChoirBase"):
        assert f'name = "{lib}"' in out
    assert lean_lib_names(out) == ["Proj", "Challenge", "Solution", "ChoirBase"]


def test_workspace_lakefile_reserved_collision() -> None:
    bad = SAMPLE_LAKEFILE + '\n[[lean_lib]]\nname = "Challenge"\n'
    with pytest.raises(ComparatorConfigError):
        workspace_lakefile(bad)


# ---------------------------------------------------------------------------
# generated roots
# ---------------------------------------------------------------------------


def test_challenge_root_imports_prefixed() -> None:
    out = challenge_root(["Proj"], "Proj.Basic")
    assert f"import {CHALLENGE_PREFIX}.Proj\n" in out
    assert f"import {CHALLENGE_PREFIX}.Proj.Basic\n" in out


def test_challenge_root_dedupes_target_module() -> None:
    out = challenge_root(["Proj"], "Proj")
    assert out.count(f"import {CHALLENGE_PREFIX}.Proj\n") == 1


def test_solution_root_imports_unprefixed() -> None:
    out = solution_root(["Proj"], "Proj.Basic")
    assert "import Proj\n" in out
    assert "import Proj.Basic\n" in out
    assert CHALLENGE_PREFIX not in out


def test_choirbase_root() -> None:
    out = choirbase_root(["Proj", "Extra"])
    assert f"import {CHALLENGE_PREFIX}.Proj\n" in out
    assert f"import {CHALLENGE_PREFIX}.Extra\n" in out


# ---------------------------------------------------------------------------
# permitted axioms
# ---------------------------------------------------------------------------


def test_permitted_axioms_defaults() -> None:
    assert permitted_axioms(VerifyConfig()) == CORE_PERMITTED_AXIOMS


def test_permitted_axioms_whitelist_union() -> None:
    cfg = VerifyConfig(
        axiom_honesty=AxiomHonestyConfig(
            policy=AxiomPolicy.WHITELIST,
            allowed_axioms=("propext", "MyProject.bigConjecture"),
        )
    )
    assert permitted_axioms(cfg) == (*CORE_PERMITTED_AXIOMS, "MyProject.bigConjecture")


def test_permitted_axioms_net_zero_ignores_allowed_list() -> None:
    cfg = VerifyConfig(
        axiom_honesty=AxiomHonestyConfig(
            policy=AxiomPolicy.NET_ZERO,
            allowed_axioms=("MyProject.bigConjecture",),
        )
    )
    assert permitted_axioms(cfg) == CORE_PERMITTED_AXIOMS


def test_permitted_axioms_report_appends_sorry() -> None:
    cfg = VerifyConfig(sorry_delta=SorryDeltaConfig(policy=SorryPolicy.REPORT))
    assert permitted_axioms(cfg) == (*CORE_PERMITTED_AXIOMS, SORRY_AXIOM)


def test_permitted_axioms_reduction_appends_sorry_under_block_policy() -> None:
    cfg = VerifyConfig()
    assert cfg.sorry_delta.policy is SorryPolicy.BLOCK
    assert permitted_axioms(cfg, is_reduction=True) == (
        *CORE_PERMITTED_AXIOMS,
        SORRY_AXIOM,
    )


def test_permitted_axioms_ordinary_submission_under_block_refuses_sorry() -> None:
    cfg = VerifyConfig()
    assert cfg.sorry_delta.policy is SorryPolicy.BLOCK
    assert SORRY_AXIOM not in permitted_axioms(cfg)


def test_permitted_axioms_reduction_and_report_list_sorry_once() -> None:
    cfg = VerifyConfig(sorry_delta=SorryDeltaConfig(policy=SorryPolicy.REPORT))
    permitted = permitted_axioms(cfg, is_reduction=True)
    assert permitted.count(SORRY_AXIOM) == 1


# ---------------------------------------------------------------------------
# config.json
# ---------------------------------------------------------------------------


def test_comparator_config_json_shape() -> None:
    raw = comparator_config_json("Proj.tgt", CORE_PERMITTED_AXIOMS)
    data = json.loads(raw)
    assert data == {
        "challenge_module": "Challenge",
        "solution_module": "Solution",
        "theorem_names": ["Proj.tgt"],
        "permitted_axioms": list(CORE_PERMITTED_AXIOMS),
        "enable_nanoda": False,
    }


# ---------------------------------------------------------------------------
# verdict classification
# ---------------------------------------------------------------------------


def test_classify_exit_zero_is_match() -> None:
    outcome, _, _ = classify_output(0, "Your solution is okay!")
    assert outcome is Outcome.MATCH


def test_classify_statement_mismatch() -> None:
    outcome, msg, _ = classify_output(
        1, "uncaught exception: Challenge and solution theorem statement do not match: 'Proj.tgt'"
    )
    assert outcome is Outcome.STATEMENT_MISMATCH
    assert "Proj.tgt" in msg


def test_classify_illegal_axiom() -> None:
    outcome, _, _ = classify_output(1, "Illegal axiom detected: 'Cheat.ax'")
    assert outcome is Outcome.ILLEGAL_AXIOM


def test_classify_missing_constant_variants() -> None:
    for text in (
        "Const not found in challenge 'Proj.helper'",
        "Const not found in solution: 'Proj.tgt'",
        "Const does not match between challenge and target 'Proj.dep'",
    ):
        outcome, _, _ = classify_output(1, text)
        assert outcome is Outcome.MISSING_CONSTANT, text


def test_classify_unknown_failure_is_tool_error() -> None:
    outcome, _, _ = classify_output(3, "Child exited with 1")
    assert outcome is Outcome.TOOL_ERROR


def test_tool_error_carries_full_output():
    output = (
        "info: building solution\n"
        "error: ./Foo.lean:3:0: unknown identifier 'bar'\n"
        "uncaught exception: Child exited with 1\n"
    )
    outcome, message, full = classify_output(1, output)
    assert outcome is Outcome.TOOL_ERROR
    # summary stays the last line, for the one-line report
    assert message == "uncaught exception: Child exited with 1"
    # but the diagnosable part must survive
    assert "unknown identifier 'bar'" in full


def test_non_tool_error_outcomes_carry_no_full_output():
    outcome, _message, full = classify_output(0, "comparator accepted\n")
    assert outcome is Outcome.MATCH
    assert full == ""


def test_missing_landrun_classifies_as_sandbox_unavailable() -> None:
    output = (
        "info: building Challenge\n"
        "could not execute external process 'landrun'\n"
        "uncaught exception: Child exited with 255\n"
    )
    outcome, message, full = classify_output(255, output)
    assert outcome is Outcome.SANDBOX_UNAVAILABLE
    assert "landrun" in message
    assert "could not execute external process" in full


def test_solution_build_failure_classifies_distinctly() -> None:
    output = (
        "info: building Challenge\n"
        "info: building Solution\n"
        "./Foo.lean:12:4: error: ring_nf made no progress on the goal\n"
        "error: build failed\n"
        "uncaught exception: Child exited with 1\n"
    )
    outcome, _message, full = classify_output(1, output)
    assert outcome is Outcome.SOLUTION_BUILD_FAILED
    assert "error: build failed" in full
    # the prover diagnostic must survive — it is the actionable part
    assert "ring_nf made no progress" in full


def test_unrecognized_failure_still_falls_back_to_tool_error() -> None:
    outcome, _message, full = classify_output(1, "something nobody has seen\n")
    assert outcome is Outcome.TOOL_ERROR
    assert "something nobody has seen" in full


def test_statement_mismatch_still_wins_over_build_noise() -> None:
    """A real verdict outranks incidental build chatter."""
    output = (
        "error: build failed\n"
        "The theorem statement do not match\n"
    )
    outcome, _message, _full = classify_output(1, output)
    assert outcome is Outcome.STATEMENT_MISMATCH


def test_statement_mismatch_still_wins_over_sandbox_noise() -> None:
    """A real verdict outranks sandbox noise, not just build noise.

    Pins the landrun pattern's position *behind* the verdict patterns — the
    build-noise test only pins `error: build failed`'s position, so without
    this a refactor hoisting the infra pattern would mask a real verdict.
    """
    output = (
        "could not execute external process 'landrun'\n"
        "The theorem statement do not match\n"
    )
    outcome, _message, _full = classify_output(1, output)
    assert outcome is Outcome.STATEMENT_MISMATCH


def test_challenge_side_build_error_detects_an_error_under_the_prefix() -> None:
    output = (
        f"./{CHALLENGE_PREFIX}/Proj/Basic.lean:1:0: error: unknown module\n"
        "error: build failed\n"
    )
    assert challenge_side_build_error(output) is True


def test_challenge_side_build_error_ignores_prefix_mentions_without_errors() -> None:
    """Sorry warnings and build-progress lines must not read as a fault.

    The challenge tree holds the task's sorried target by construction, so
    `warning: declaration uses 'sorry'` under the prefix appears on every
    run; a bare-mention check would flag every PR.
    """
    output = (
        f"info: [2/9] Built {CHALLENGE_PREFIX}.Proj.Basic\n"
        f"./{CHALLENGE_PREFIX}/Proj/Basic.lean:3:8: warning: declaration uses "
        "'sorry'\n"
        "./Proj/Basic.lean:3:20: error: unknown identifier 'foo'\n"
        "error: build failed\n"
    )
    assert challenge_side_build_error(output) is False


def test_challenge_side_build_error_is_false_on_empty_output() -> None:
    assert challenge_side_build_error("") is False


# ---------------------------------------------------------------------------
# private declarations in a target's statement
# ---------------------------------------------------------------------------

_SOURCE = """\
import Mathlib

namespace Proj

private def IsGreedyRule (d : ℝ) (pick : Nat → Nat) : Prop :=
  ∀ n, pick n = n

def PublicPred (d : ℝ) : Prop := d > 0

private theorem helper_bound (d : ℝ) : d ≤ d := le_refl d

theorem exists_greedy_rule (d : ℝ) (hd : 0 < d) :
    ∃ pick : Nat → Nat, IsGreedyRule d pick := by
  sorry

theorem uses_only_public (d : ℝ) : PublicPred d ∨ d ≤ 0 := by
  sorry

theorem mentions_private_in_body_only (d : ℝ) : d ≤ d := by
  exact helper_bound d

end Proj
"""


def test_private_decl_names_finds_private_declarations() -> None:
    assert private_decl_names(_SOURCE) == {"IsGreedyRule", "helper_bound"}


def test_private_decl_names_is_empty_without_any() -> None:
    assert private_decl_names("def f : Nat := 1\ntheorem g : True := trivial\n") == set()


def test_a_private_declaration_in_the_statement_is_reported() -> None:
    assert private_names_in_statement(_SOURCE, "Proj.exists_greedy_rule") == [
        "IsGreedyRule"
    ]


def test_a_public_only_statement_reports_nothing() -> None:
    assert private_names_in_statement(_SOURCE, "Proj.uses_only_public") == []


def test_a_private_name_used_only_in_the_body_is_not_reported() -> None:
    """The body is a kernel-checked proof; only the statement has to match."""
    assert (
        private_names_in_statement(_SOURCE, "Proj.mentions_private_in_body_only") == []
    )


def test_an_absent_declaration_reports_nothing() -> None:
    assert private_names_in_statement(_SOURCE, "Proj.not_here") == []


def test_a_private_target_does_not_report_itself() -> None:
    assert private_names_in_statement(_SOURCE, "Proj.helper_bound") == []


def test_declaration_signature_stops_at_the_body() -> None:
    signature = declaration_signature(_SOURCE, "Proj.exists_greedy_rule")
    assert signature is not None
    assert "IsGreedyRule d pick" in signature
    assert "sorry" not in signature


def test_declaration_signature_accepts_an_unqualified_name() -> None:
    assert declaration_signature(_SOURCE, "exists_greedy_rule") is not None


def test_a_substring_of_a_longer_name_is_not_a_match() -> None:
    source = """\
private def Rule : Prop := True

theorem t : IsRuleLike → True := by sorry
"""
    assert private_names_in_statement(source, "t") == []


def test_modifiers_between_private_and_the_keyword_are_tolerated() -> None:
    source = "private noncomputable def Weird : Nat := 0\n"
    assert private_decl_names(source) == {"Weird"}


# ---------------------------------------------------------------------------
# anonymous instances whose generated name reads the module root
# ---------------------------------------------------------------------------

_INSTANCES = """\
import Mathlib

namespace Proj

def uniformPerm (n : ℕ) : Measure (Equiv.Perm (Fin n)) := 0

instance : MeasurableSpace (Equiv.Perm (Fin n)) := ⊤

instance instPermMeasurable : MeasurableSpace (Equiv.Perm (Fin n)) := ⊤

instance : IsProbabilityMeasure (uniformPerm n) := by sorry

end Proj
"""


def test_an_anonymous_instance_over_foreign_types_is_reported() -> None:
    headers = suffix_divergent_instances(_INSTANCES, declared_names(_INSTANCES))
    assert headers == ["instance : MeasurableSpace (Equiv.Perm (Fin n))"]


def test_a_named_instance_is_not_reported() -> None:
    assert "instPermMeasurable" not in " ".join(
        suffix_divergent_instances(_INSTANCES, declared_names(_INSTANCES))
    )


def test_an_anonymous_instance_mentioning_a_project_name_is_not_reported() -> None:
    assert "IsProbabilityMeasure" not in " ".join(
        suffix_divergent_instances(_INSTANCES, declared_names(_INSTANCES))
    )


def test_a_project_name_in_the_body_does_not_rescue_the_header() -> None:
    source = "def helper : Nat := 0\ninstance : Inhabited Nat := ⟨helper⟩\n"
    assert suffix_divergent_instances(source, {"helper"}) == [
        "instance : Inhabited Nat"
    ]


def test_a_where_body_ends_the_header() -> None:
    source = "instance : Inhabited Nat where\n  default := 0\n"
    assert suffix_divergent_instances(source, set()) == ["instance : Inhabited Nat"]


def test_a_substring_of_a_project_name_does_not_rescue_the_header() -> None:
    source = "instance : Inhabited Nat := ⟨0⟩\n"
    assert suffix_divergent_instances(source, {"Na"}) == ["instance : Inhabited Nat"]


def test_declared_names_collects_public_and_private_but_not_instances() -> None:
    assert declared_names(_INSTANCES) == {"uniformPerm"}


def test_imported_modules_reads_the_import_header() -> None:
    assert imported_modules("import Mathlib\nimport Proj.Basic\n\ndef x := 0\n") == [
        "Mathlib",
        "Proj.Basic",
    ]
