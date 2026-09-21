"""TaskRecord validation for the live task types (prove/golf/index).

Historically covered the type-branched target validation from design
note 11 §6.1 (`review` tasks pointed at a PR via `target_pr` instead of
a file); spec D1 removed the review task type, so every remaining type
takes the same file+decl shape.
"""

import pytest
from pydantic import ValidationError

from gate.state.task_record import ProjectRef, TaskRecord, TaskType
from orchestrator.tasks.serialize import body_to_task, task_to_body

REF = {"repo": "acme/proofs", "commit": "a" * 40, "toolchain": "leanprover/lean4:v4.31.0"}


def make(**kw) -> TaskRecord:
    base = {"choir-task-version": 1, "project_ref": ProjectRef(**REF), "deps": []}
    return TaskRecord(**{**base, **kw})


def test_task_type_vocabulary_is_exactly_the_live_types() -> None:
    """Retired 2026-08-18: formalize (spec D2 forbids worker-authored
    statements), draft and refactor (no plan, zero live use), review (spec
    D1 removed the review layer entirely). `index` stays for the Phase 5
    coherence indexer.
    """
    assert {t.value for t in TaskType} == {"prove", "golf", "index"}


class TestProveTasks:
    def test_prove_task_still_requires_lean_target(self):
        rec = make(type="prove", target_file="Foo.lean", target_decl="Foo.bar")
        assert rec.target_file == "Foo.lean"

    def test_prove_task_requires_target_file(self):
        with pytest.raises(ValidationError, match="requires target_file"):
            make(type="prove", target_decl="Foo.bar")

    def test_schema_is_prover_blind_any_extension_accepted(self):
        # design note 12 §5: the `.lean`-only rule moved to intake (with
        # profile context); the schema itself only enforces path hygiene.
        rec = make(type="prove", target_file="Foo.v", target_decl="Foo.bar")
        assert rec.target_file == "Foo.v"

    def test_schema_accepts_non_lean_extensions(self):
        rec = make(type="prove", target_file="Foo.thy", target_decl="Foo.bar")
        assert rec.target_file == "Foo.thy"

    def test_target_decl_with_a_trailing_dot_is_rejected(self):
        # A trailing dot splits to an empty leaf downstream, in
        # `compare_reduction`'s own comparison — reject it at the
        # source rather than rely on that function's fail-closed rule
        # alone.
        with pytest.raises(ValidationError, match="target_decl"):
            make(type="prove", target_file="Foo.lean", target_decl="Foo.bar.")

    def test_target_decl_accepts_unicode_identifiers(self):
        # Lean identifiers routinely use non-ASCII letters (Greek,
        # subscripts); a project using them must still be able to
        # declare a task against one.
        rec = make(type="prove", target_file="Foo.lean", target_decl="NS.α_β")
        assert rec.target_decl == "NS.α_β"


class TestGolfTasks:
    # `golf` rides the generic branch. Pinned because the golf playbook
    # relies on it (spec 2026-07-18).

    def test_golf_task_validates_with_file_and_decl(self):
        rec = make(type="golf", target_file="Foo.lean", target_decl="Foo.bar")
        assert rec.type == TaskType.GOLF

    def test_golf_task_requires_file_and_decl(self):
        with pytest.raises(ValidationError, match="requires target_file"):
            make(type="golf")


class TestSerializeRoundTrip:
    def test_golf_task_round_trips(self):
        rec = make(type="golf", target_file="Foo.lean", target_decl="Foo.bar")
        body = task_to_body(rec, "Golf the proof of Foo.bar — the statement stays fixed.")
        parsed, prose = body_to_task(body)
        assert parsed == rec
        assert "statement stays fixed" in prose
