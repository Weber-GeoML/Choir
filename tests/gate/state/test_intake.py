"""Tests for `gate.state.intake.parse_issue_body`.

Coverage targets the schema's failure modes,
plus the happy path and normalization rules. Each test asserts the stable
error code(s) and field path, not just "an error occurred."
"""

from __future__ import annotations

import textwrap

from gate.state import intake
from gate.state.intake import (
    ERROR_INVALID_VALUE,
    ERROR_INVALID_YAML,
    ERROR_MISSING_FIELD,
    ERROR_NO_FRONTMATTER,
    ERROR_REPO_MISMATCH,
    ERROR_TARGET_FILE_EXTENSION,
    ERROR_TYPE_MISMATCH,
    ERROR_UNKNOWN_FIELD,
    ERROR_UNKNOWN_VERSION,
    ERROR_UNTERMINATED_FRONTMATTER,
    ParseError,
    ParseSuccess,
    parse_issue_body,
)
from gate.state.task_record import TaskType


def _valid_body(**overrides: str) -> str:
    """A minimal valid issue body. Override individual lines via kwargs (rarely used)."""
    body = textwrap.dedent(
        """\
        ---
        choir-task-version: 1
        type: prove
        target_file: MyProj/Foo.lean
        target_decl: MyProj.Foo.add_comm
        project_ref:
          repo: org/myproj
          commit: 1a2b3c4d
          toolchain: leanprover/lean4:v4.x.y
        deps: []
        ---

        ## Statement

        theorem MyProj.Foo.add_comm ...
        """
    )
    for key, value in overrides.items():
        body = body.replace(key, value)
    return body


def _errors_by_code(result: list[ParseError]) -> dict[str, ParseError]:
    return {e.code: e for e in result}


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path_returns_record_and_prose() -> None:
    result = parse_issue_body(_valid_body())
    assert isinstance(result, ParseSuccess)
    assert result.record.type == TaskType.PROVE
    assert result.record.target_file == "MyProj/Foo.lean"
    assert result.record.target_decl == "MyProj.Foo.add_comm"
    assert result.record.project_ref.repo == "org/myproj"
    assert result.record.project_ref.commit == "1a2b3c4d"
    assert result.record.deps == []
    assert result.record.blueprint_ref is None
    assert "Statement" in result.body_prose
    assert result.body_prose.startswith("## Statement")


def test_blueprint_ref_optional_and_passed_through() -> None:
    body = _valid_body().replace(
        "deps: []",
        "deps: []\nblueprint_ref: blueprint/foo.tex#add_comm",
    )
    result = parse_issue_body(body)
    assert isinstance(result, ParseSuccess)
    assert result.record.blueprint_ref == "blueprint/foo.tex#add_comm"


def test_deps_populated() -> None:
    body = _valid_body().replace("deps: []", "deps: [12, 34, 56]")
    result = parse_issue_body(body)
    assert isinstance(result, ParseSuccess)
    assert result.record.deps == [12, 34, 56]


# ---------------------------------------------------------------------------
# Front-matter extraction
# ---------------------------------------------------------------------------


def test_missing_frontmatter() -> None:
    result = parse_issue_body("just some prose, no yaml block here\n")
    assert isinstance(result, list)
    assert _errors_by_code(result)[ERROR_NO_FRONTMATTER]


def test_unterminated_frontmatter() -> None:
    body = "---\nchoir-task-version: 1\ntype: prove\n"
    result = parse_issue_body(body)
    assert isinstance(result, list)
    assert _errors_by_code(result)[ERROR_UNTERMINATED_FRONTMATTER]


def test_blank_lines_before_frontmatter_are_tolerated() -> None:
    body = "\n\n" + _valid_body()
    result = parse_issue_body(body)
    assert isinstance(result, ParseSuccess)


def test_leading_html_comment_is_tolerated() -> None:
    # GitHub's issue template prepends a multi-line instructions comment; the
    # body a contributor submits via the UI then begins with it. Intake must
    # still locate the front-matter (regression: it used to fail no_frontmatter).
    body = (
        "<!--\n"
        "This is a Choir task. Replace OWNER/REPO, SHA, target_* before submitting.\n"
        "-->\n\n"
    ) + _valid_body()
    result = parse_issue_body(body)
    assert isinstance(result, ParseSuccess)


def test_invalid_yaml_inside_frontmatter() -> None:
    body = "---\nchoir-task-version: 1\n  bad indent: oops\n---\n"
    result = parse_issue_body(body)
    assert isinstance(result, list)
    assert _errors_by_code(result)[ERROR_INVALID_YAML]


def test_frontmatter_not_a_mapping() -> None:
    body = "---\n- just\n- a\n- list\n---\n"
    result = parse_issue_body(body)
    assert isinstance(result, list)
    assert _errors_by_code(result)[ERROR_INVALID_YAML]


# ---------------------------------------------------------------------------
# Version dispatch
# ---------------------------------------------------------------------------


def test_missing_version_is_missing_field() -> None:
    body = _valid_body().replace("choir-task-version: 1\n", "")
    result = parse_issue_body(body)
    assert isinstance(result, list)
    err = _errors_by_code(result)[ERROR_MISSING_FIELD]
    assert err.field == "choir-task-version"


def test_unknown_version_is_rejected() -> None:
    body = _valid_body().replace("choir-task-version: 1", "choir-task-version: 2")
    result = parse_issue_body(body)
    assert isinstance(result, list)
    err = _errors_by_code(result)[ERROR_UNKNOWN_VERSION]
    assert err.field == "choir-task-version"


def test_version_must_be_an_integer() -> None:
    body = _valid_body().replace("choir-task-version: 1", "choir-task-version: 'one'")
    result = parse_issue_body(body)
    assert isinstance(result, list)
    err = _errors_by_code(result)[ERROR_TYPE_MISMATCH]
    assert err.field == "choir-task-version"


def test_version_boolean_is_a_type_mismatch_not_an_int() -> None:
    # PyYAML maps `true` to bool, and bool is a subclass of int in Python.
    # We don't want `true` to be accepted as version 1.
    body = _valid_body().replace("choir-task-version: 1", "choir-task-version: true")
    result = parse_issue_body(body)
    assert isinstance(result, list)
    err = _errors_by_code(result)[ERROR_TYPE_MISMATCH]
    assert err.field == "choir-task-version"


# ---------------------------------------------------------------------------
# Missing / unknown fields
# ---------------------------------------------------------------------------


def test_missing_required_field() -> None:
    body = _valid_body().replace("type: prove\n", "")
    result = parse_issue_body(body)
    assert isinstance(result, list)
    err = _errors_by_code(result)[ERROR_MISSING_FIELD]
    assert err.field == "type"


def test_unknown_field_is_rejected() -> None:
    body = _valid_body().replace("deps: []", "deps: []\nbogus_field: oops")
    result = parse_issue_body(body)
    assert isinstance(result, list)
    err = _errors_by_code(result)[ERROR_UNKNOWN_FIELD]
    assert err.field == "bogus_field"


def test_errors_are_accumulated() -> None:
    # Missing type and unknown field — both should show up.
    body = (
        _valid_body().replace("type: prove\n", "").replace("deps: []", "deps: []\nbogus: 1")
    )
    result = parse_issue_body(body)
    assert isinstance(result, list)
    codes = {e.code for e in result}
    assert ERROR_MISSING_FIELD in codes
    assert ERROR_UNKNOWN_FIELD in codes


# ---------------------------------------------------------------------------
# `type` enum
# ---------------------------------------------------------------------------


def test_invalid_type_value() -> None:
    body = _valid_body().replace("type: prove", "type: shenanigans")
    result = parse_issue_body(body)
    assert isinstance(result, list)
    err = _errors_by_code(result)[ERROR_INVALID_VALUE]
    assert err.field == "type"


def test_retired_types_are_rejected() -> None:
    # Retired 2026-08-18: formalize (spec D2 forbids worker-authored
    # statements), draft and refactor (no plan, zero live use), review
    # (spec D1 removed the whole distributed-review layer it belonged to
    # — the only one of these four a real protocol-4 repo can actually
    # be holding open issues for).
    for dead in ("formalize", "draft", "refactor", "review"):
        body = _valid_body().replace("type: prove", f"type: {dead}")
        result = parse_issue_body(body)
        assert isinstance(result, list), f"{dead} must no longer parse"
        assert _errors_by_code(result)[ERROR_INVALID_VALUE].field == "type"


# ---------------------------------------------------------------------------
# target_file
# ---------------------------------------------------------------------------


def test_target_file_absolute_path_rejected() -> None:
    body = _valid_body().replace("target_file: MyProj/Foo.lean", "target_file: /MyProj/Foo.lean")
    result = parse_issue_body(body)
    assert isinstance(result, list)
    assert any(e.field == "target_file" for e in result)


def test_target_file_dotdot_rejected() -> None:
    body = _valid_body().replace(
        "target_file: MyProj/Foo.lean", "target_file: MyProj/../Foo.lean"
    )
    result = parse_issue_body(body)
    assert isinstance(result, list)
    assert any(e.field == "target_file" for e in result)


def test_target_file_backslash_rejected() -> None:
    body = _valid_body().replace(
        "target_file: MyProj/Foo.lean", "target_file: MyProj\\Foo.lean"
    )
    result = parse_issue_body(body)
    assert isinstance(result, list)
    assert any(e.field == "target_file" for e in result)


def test_target_file_extension_not_checked_by_default() -> None:
    # TaskRecord is prover-blind (design note 12 §5); with no
    # `allowed_extensions`, any suffix parses successfully.
    body = _valid_body().replace("target_file: MyProj/Foo.lean", "target_file: MyProj/Foo.txt")
    result = parse_issue_body(body)
    assert isinstance(result, ParseSuccess)
    assert result.record.target_file == "MyProj/Foo.txt"


def test_target_file_extension_rejected_when_not_in_allowed_set() -> None:
    body = _valid_body().replace("target_file: MyProj/Foo.lean", "target_file: MyProj/Foo.thy")
    result = parse_issue_body(body, allowed_extensions=(".lean",))
    assert isinstance(result, list)
    err = _errors_by_code(result)[ERROR_TARGET_FILE_EXTENSION]
    assert err.field == "target_file"
    assert ".lean" in err.message


def test_target_file_extension_accepted_when_in_allowed_set() -> None:
    body = _valid_body().replace("target_file: MyProj/Foo.lean", "target_file: MyProj/Foo.thy")
    result = parse_issue_body(body, allowed_extensions=(".thy", ".lean"))
    assert isinstance(result, ParseSuccess)
    assert result.record.target_file == "MyProj/Foo.thy"


def test_target_file_extension_check_skipped_when_none() -> None:
    body = _valid_body().replace("target_file: MyProj/Foo.lean", "target_file: MyProj/Foo.weird")
    result = parse_issue_body(body, allowed_extensions=None)
    assert isinstance(result, ParseSuccess)


def test_target_file_extension_check_is_additive_not_a_schema_replacement() -> None:
    # A record that fails schema validation for an unrelated reason must
    # short-circuit before the extension check ever runs — the extension
    # check is layered on top of an otherwise-valid record, not a
    # substitute for schema errors.
    body = (
        _valid_body()
        .replace("type: prove\n", "")
        .replace("target_file: MyProj/Foo.lean", "target_file: MyProj/Foo.thy")
    )
    result = parse_issue_body(body, allowed_extensions=(".lean",))
    assert isinstance(result, list)
    codes = {e.code for e in result}
    assert ERROR_MISSING_FIELD in codes
    assert ERROR_TARGET_FILE_EXTENSION not in codes


def test_target_file_extension_error_accumulates_with_repo_mismatch() -> None:
    body = _valid_body().replace("target_file: MyProj/Foo.lean", "target_file: MyProj/Foo.thy")
    result = parse_issue_body(
        body, expected_repo="other-org/myproj", allowed_extensions=(".lean",)
    )
    assert isinstance(result, list)
    codes = {e.code for e in result}
    assert ERROR_REPO_MISMATCH in codes
    assert ERROR_TARGET_FILE_EXTENSION in codes


def test_target_file_normalizes_leading_dot_slash() -> None:
    body = _valid_body().replace("target_file: MyProj/Foo.lean", "target_file: ./MyProj/Foo.lean")
    result = parse_issue_body(body)
    assert isinstance(result, ParseSuccess)
    assert result.record.target_file == "MyProj/Foo.lean"


# ---------------------------------------------------------------------------
# target_decl
# ---------------------------------------------------------------------------


def test_target_decl_with_invalid_chars_rejected() -> None:
    body = _valid_body().replace(
        "target_decl: MyProj.Foo.add_comm", "target_decl: My Proj.Foo.add_comm"
    )
    result = parse_issue_body(body)
    assert isinstance(result, list)
    assert any(e.field == "target_decl" for e in result)


def test_target_decl_starting_with_digit_rejected() -> None:
    body = _valid_body().replace(
        "target_decl: MyProj.Foo.add_comm", "target_decl: 1MyProj.Foo"
    )
    result = parse_issue_body(body)
    assert isinstance(result, list)
    assert any(e.field == "target_decl" for e in result)


def test_target_decl_with_apostrophe_allowed() -> None:
    # Mathlib uses primed variants regularly.
    body = _valid_body().replace(
        "target_decl: MyProj.Foo.add_comm",
        "target_decl: MyProj.Foo.add_comm'",
    )
    result = parse_issue_body(body)
    assert isinstance(result, ParseSuccess)
    assert result.record.target_decl == "MyProj.Foo.add_comm'"


# ---------------------------------------------------------------------------
# project_ref
# ---------------------------------------------------------------------------


def test_commit_too_short_rejected() -> None:
    body = _valid_body().replace("commit: 1a2b3c4d", "commit: 1a2b")
    result = parse_issue_body(body)
    assert isinstance(result, list)
    assert any(e.field == "project_ref.commit" for e in result)


def test_commit_uppercase_normalized_to_lowercase() -> None:
    body = _valid_body().replace("commit: 1a2b3c4d", "commit: 1A2B3C4D")
    result = parse_issue_body(body)
    assert isinstance(result, ParseSuccess)
    assert result.record.project_ref.commit == "1a2b3c4d"


def test_commit_non_hex_rejected() -> None:
    body = _valid_body().replace("commit: 1a2b3c4d", "commit: zzzzzzz")
    result = parse_issue_body(body)
    assert isinstance(result, list)
    assert any(e.field == "project_ref.commit" for e in result)


def test_repo_wrong_shape_rejected() -> None:
    body = _valid_body().replace("repo: org/myproj", "repo: justname")
    result = parse_issue_body(body)
    assert isinstance(result, list)
    assert any(e.field == "project_ref.repo" for e in result)


def test_repo_org_lowercased() -> None:
    body = _valid_body().replace("repo: org/myproj", "repo: ORG/MyProj")
    result = parse_issue_body(body)
    assert isinstance(result, ParseSuccess)
    # Org lowercased, repo name preserved.
    assert result.record.project_ref.repo == "org/MyProj"


def test_repo_mismatch_against_expected() -> None:
    result = parse_issue_body(_valid_body(), expected_repo="other-org/myproj")
    assert isinstance(result, list)
    err = _errors_by_code(result)[ERROR_REPO_MISMATCH]
    assert err.field == "project_ref.repo"


def test_repo_match_against_expected_passes() -> None:
    # Case-insensitive match.
    result = parse_issue_body(_valid_body(), expected_repo="ORG/myproj")
    assert isinstance(result, ParseSuccess)


# ---------------------------------------------------------------------------
# deps
# ---------------------------------------------------------------------------


def test_deps_negative_rejected() -> None:
    body = _valid_body().replace("deps: []", "deps: [-1]")
    result = parse_issue_body(body)
    assert isinstance(result, list)
    assert any(e.field and e.field.startswith("deps") for e in result)


def test_deps_zero_rejected() -> None:
    body = _valid_body().replace("deps: []", "deps: [0, 5]")
    result = parse_issue_body(body)
    assert isinstance(result, list)
    assert any(e.field and e.field.startswith("deps") for e in result)


def test_deps_non_int_rejected() -> None:
    body = _valid_body().replace("deps: []", "deps: ['not a number']")
    result = parse_issue_body(body)
    assert isinstance(result, list)
    assert any(e.field and e.field.startswith("deps") for e in result)


# ---------------------------------------------------------------------------
# Smoke: error-code constants are stable strings
# ---------------------------------------------------------------------------


def test_error_codes_are_strings() -> None:
    # If anyone refactors and accidentally converts one to an enum, this catches it.
    assert isinstance(intake.ERROR_NO_FRONTMATTER, str)
    assert isinstance(intake.ERROR_MISSING_FIELD, str)
    assert isinstance(intake.ERROR_UNKNOWN_VERSION, str)
