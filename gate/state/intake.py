"""Issue-body intake: extract YAML front-matter and validate against TaskRecord.

This is the entry point called from the `issue-intake.yml` workflow. The
workflow is responsible for posting the result back to GitHub; this module
is intentionally pure (no I/O, no API calls).

`gate.state.task_record` holds the schema this validates against.
"""

from __future__ import annotations

from dataclasses import dataclass

import yaml
from pydantic import ValidationError

from gate.state.task_record import TaskRecord

# Stable error codes — documented in design note 02. Keep these names stable
# across schema versions; downstream tooling (issue-template forms, future
# linters, the intake-ack comment renderer) may pattern-match on them.
ERROR_NO_FRONTMATTER = "no_frontmatter"
ERROR_UNTERMINATED_FRONTMATTER = "unterminated_frontmatter"
ERROR_INVALID_YAML = "invalid_yaml"
ERROR_MISSING_FIELD = "missing_field"
ERROR_UNKNOWN_FIELD = "unknown_field"
ERROR_TYPE_MISMATCH = "type_mismatch"
ERROR_INVALID_VALUE = "invalid_value"
ERROR_UNKNOWN_VERSION = "unknown_version"
ERROR_REPO_MISMATCH = "repo_mismatch"
ERROR_TARGET_FILE_EXTENSION = "target_file_extension"


@dataclass(frozen=True)
class ParseError:
    """A single validation problem with a parsed issue body."""

    code: str
    message: str
    field: str | None = None
    hint: str | None = None


@dataclass(frozen=True)
class ParseSuccess:
    """A successfully parsed issue. `body_prose` is what's after the front-matter."""

    record: TaskRecord
    body_prose: str


ParseResult = ParseSuccess | list[ParseError]


def parse_issue_body(
    body: str,
    *,
    expected_repo: str | None = None,
    allowed_extensions: tuple[str, ...] | None = None,
) -> ParseResult:
    """Parse a GitHub issue body.

    Returns `ParseSuccess` on a fully valid record, otherwise a list of
    `ParseError`. Errors are accumulated where possible (Pydantic validation
    returns all errors at once); errors that prevent further parsing (no
    front-matter, invalid YAML, unknown schema version) short-circuit.

    `expected_repo` (`"owner/name"`) is optional; when provided, a
    `repo_mismatch` error is added if `project_ref.repo` doesn't match.

    `allowed_extensions` is optional; when provided and the record has a
    non-`None` `target_file`, its suffix must be in the set or a
    `target_file_extension` error is added. `TaskRecord` itself is
    prover-blind (design note 12 §5) — the `.lean`-only rule moved here so
    intake can check the *selected project's* prover profile instead of a
    hard-coded suffix. `None` means "no check" (library callers that don't
    care about prover profiles keep working unchanged).
    """
    extracted = _extract_frontmatter(body)
    if isinstance(extracted, ParseError):
        return [extracted]
    yaml_text, prose = extracted

    try:
        data = yaml.safe_load(yaml_text)
    except yaml.YAMLError as e:
        return [ParseError(code=ERROR_INVALID_YAML, message=f"YAML parse failed: {e}")]

    if not isinstance(data, dict):
        return [
            ParseError(
                code=ERROR_INVALID_YAML,
                message="front-matter must be a YAML mapping (key: value pairs)",
            )
        ]

    version = data.get("choir-task-version")
    if version is None:
        return [
            ParseError(
                code=ERROR_MISSING_FIELD,
                field="choir-task-version",
                message="every Choir task must declare 'choir-task-version'",
            )
        ]
    if not isinstance(version, int) or isinstance(version, bool):
        return [
            ParseError(
                code=ERROR_TYPE_MISMATCH,
                field="choir-task-version",
                message=(
                    f"choir-task-version must be an integer, got "
                    f"{type(version).__name__}"
                ),
            )
        ]
    if version != 1:
        return [
            ParseError(
                code=ERROR_UNKNOWN_VERSION,
                field="choir-task-version",
                message=(
                    f"unsupported schema version: {version} "
                    f"(this orchestrator supports version 1)"
                ),
            )
        ]

    try:
        record = TaskRecord.model_validate(data)
    except ValidationError as e:
        return _translate_validation_errors(e)

    errors: list[ParseError] = []
    if expected_repo is not None and record.project_ref.repo.lower() != expected_repo.lower():
        errors.append(
            ParseError(
                code=ERROR_REPO_MISMATCH,
                field="project_ref.repo",
                message=(
                    f"project_ref.repo is {record.project_ref.repo!r} but this issue "
                    f"lives in {expected_repo!r}"
                ),
            )
        )

    if (
        allowed_extensions is not None
        and record.target_file is not None
        and not record.target_file.endswith(allowed_extensions)
    ):
        allowed_str = ", ".join(allowed_extensions)
        errors.append(
            ParseError(
                code=ERROR_TARGET_FILE_EXTENSION,
                field="target_file",
                message=(
                    f"target_file {record.target_file!r} does not end in an "
                    f"extension this project's prover accepts: {allowed_str}"
                ),
            )
        )

    if errors:
        return errors
    return ParseSuccess(record=record, body_prose=prose)


def _extract_frontmatter(body: str) -> tuple[str, str] | ParseError:
    """Find the YAML front-matter block delimited by `---` lines.

    The opening `---` must be the first content line, after any leading blank
    lines or HTML comments. (GitHub issue templates prepend an instructions
    `<!-- ... -->` comment, so a UI-submitted body begins with it; that must not
    hide the front-matter.) Returns `(yaml_text, prose_after)` or a `ParseError`.
    """
    lines = body.splitlines()

    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped == "":
            i += 1
            continue
        if stripped.startswith("<!--"):
            # Skip the whole comment block (it may span multiple lines).
            while i < len(lines) and "-->" not in lines[i]:
                i += 1
            i += 1  # consume the line carrying '-->'
            continue
        break
    if i >= len(lines) or lines[i].strip() != "---":
        return ParseError(
            code=ERROR_NO_FRONTMATTER,
            message=(
                "issue body must begin with a YAML front-matter block delimited by "
                "'---' lines (after any blank lines)"
            ),
        )

    start = i + 1
    j = start
    while j < len(lines) and lines[j].strip() != "---":
        j += 1
    if j >= len(lines):
        return ParseError(
            code=ERROR_UNTERMINATED_FRONTMATTER,
            message="YAML front-matter block was opened with '---' but never closed",
        )

    yaml_text = "\n".join(lines[start:j])
    prose = "\n".join(lines[j + 1 :]).lstrip("\n")
    return yaml_text, prose


_PYDANTIC_CODE_MAP = {
    "missing": ERROR_MISSING_FIELD,
    "extra_forbidden": ERROR_UNKNOWN_FIELD,
    "literal_error": ERROR_INVALID_VALUE,
    "enum": ERROR_INVALID_VALUE,
    "value_error": ERROR_INVALID_VALUE,
    "string_pattern_mismatch": ERROR_INVALID_VALUE,
    "int_type": ERROR_TYPE_MISMATCH,
    "string_type": ERROR_TYPE_MISMATCH,
    "list_type": ERROR_TYPE_MISMATCH,
    "model_type": ERROR_TYPE_MISMATCH,
    "dict_type": ERROR_TYPE_MISMATCH,
    "bool_type": ERROR_TYPE_MISMATCH,
}


def _translate_validation_errors(exc: ValidationError) -> list[ParseError]:
    """Translate Pydantic `ValidationError` to a `ParseError` list with stable codes."""
    out: list[ParseError] = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"])
        code = _PYDANTIC_CODE_MAP.get(err["type"], err["type"])
        out.append(ParseError(code=code, field=loc or None, message=err["msg"]))
    return out
