"""Tests for `orchestrator.tasks.serialize`.

The load-bearing property: every body produced by `task_to_body` must
round-trip through the intake parser. If this ever fails the intake
workflow would reject issues that the maintainer library created.
"""

from __future__ import annotations

import pytest

from gate.state.intake import ParseSuccess, parse_issue_body
from gate.state.task_record import ProjectRef, TaskRecord, TaskType
from orchestrator.tasks.serialize import body_to_task, task_to_body


def _record(**overrides) -> TaskRecord:  # type: ignore[no-untyped-def]
    defaults = dict(
        choir_task_version=1,
        type=TaskType.PROVE,
        target_file="Sample/Foo.lean",
        target_decl="Sample.foo",
        project_ref=ProjectRef(
            repo="alice/proj",
            commit="abcdef1234567",
            toolchain="leanprover/lean4:v4.5.0",
        ),
        deps=[],
        blueprint_ref=None,
    )
    defaults.update(overrides)
    return TaskRecord(**defaults)


def test_task_to_body_roundtrips_through_intake() -> None:
    task = _record()
    body = task_to_body(task, "Prove that 1+1=2.")
    result = parse_issue_body(body)
    assert isinstance(result, ParseSuccess)
    assert result.record == task
    assert "Prove that 1+1=2." in result.body_prose


def test_task_to_body_empty_prose() -> None:
    task = _record()
    body = task_to_body(task)
    result = parse_issue_body(body)
    assert isinstance(result, ParseSuccess)
    assert result.record == task
    assert result.body_prose.strip() == ""


def test_task_to_body_uses_wire_field_name() -> None:
    # The schema's wire name is `choir-task-version` (kebab). Python
    # attribute is `choir_task_version`. If by_alias dump regresses,
    # intake will reject the body with `no_frontmatter` / `missing_field`.
    task = _record()
    body = task_to_body(task)
    assert "choir-task-version: 1" in body
    assert "choir_task_version" not in body


def test_task_to_body_preserves_deps_and_blueprint() -> None:
    task = _record(deps=[7, 12], blueprint_ref="docs/blueprint/foo.md")
    body = task_to_body(task)
    result = parse_issue_body(body)
    assert isinstance(result, ParseSuccess)
    assert result.record.deps == [7, 12]
    assert result.record.blueprint_ref == "docs/blueprint/foo.md"


def test_task_to_body_every_task_type_roundtrips() -> None:
    # FormalQualBench-style use case: overseer batches `type: prove`
    # tasks. We should be confident that every TaskType in the schema
    # serializes back cleanly.
    for t in TaskType:
        task = _record(type=t)
        body = task_to_body(task)
        result = parse_issue_body(body)
        assert isinstance(result, ParseSuccess), f"failed for type={t}"
        assert result.record.type == t


def test_body_to_task_returns_record_and_prose() -> None:
    task = _record()
    body = task_to_body(task, "describe me")
    record, prose = body_to_task(body)
    assert record == task
    assert prose.strip() == "describe me"


def test_body_to_task_raises_on_malformed() -> None:
    with pytest.raises(ValueError, match="failed intake parse"):
        body_to_task("no front-matter here")


def test_body_to_task_error_message_includes_intake_code() -> None:
    # The maintainer needs to know WHY parse failed; the error should
    # include the stable intake error code, not just a generic message.
    bad_body = (
        "---\n"
        "choir-task-version: 1\n"
        "type: not_a_real_type\n"
        "---\n"
    )
    with pytest.raises(ValueError) as exc:
        body_to_task(bad_body)
    msg = str(exc.value)
    # Either `invalid_value` (Pydantic enum) or `missing_field` —
    # what matters is that some stable code is present.
    assert any(code in msg for code in ("invalid_value", "missing_field", "type_mismatch"))
