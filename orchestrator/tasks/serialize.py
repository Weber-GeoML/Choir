"""TaskRecord ↔ issue-body serialization.

The intake workflow's parser
(`gate.state.intake.parse_issue_body`) is the canonical reader.
The functions here produce bodies that round-trip through it: every
issue created via `task_to_body` validates cleanly when the intake
workflow runs.

Format::

    ---
    choir-task-version: 1
    type: prove
    target_file: …
    target_decl: …
    project_ref:
      repo: …
      commit: …
      toolchain: …
    deps: []
    ---

    <free-form prose>

Field order in the YAML is stable to keep diffs readable when
maintainers regenerate batches — `yaml.safe_dump` with `sort_keys=False`.
Optional fields that are unset (`blueprint_ref`) are omitted from the
body rather than serialized as `null`.
"""

from __future__ import annotations

import yaml

from gate.state.intake import (
    ParseError,
    ParseSuccess,
    parse_issue_body,
)
from gate.state.task_record import TaskRecord


def task_to_body(task: TaskRecord, prose: str = "") -> str:
    """Serialize a TaskRecord + free-form prose into an issue body.

    The returned string is suitable for `gh issue create --body`. The
    body always round-trips through `intake.parse_issue_body` —
    construction is via `TaskRecord.model_dump(by_alias=True)`, which
    matches the parser's expected field names exactly.

    `prose` is the human-readable description that goes after the
    front-matter (statement of the problem, references, hints). Use
    "" for issues that don't need a description; the front-matter
    alone is a valid Choir task.
    """
    # Use by_alias=True so `choir_task_version` is serialized as
    # `choir-task-version` (the schema's wire name).
    raw = task.model_dump(by_alias=True, mode="json", exclude_none=True)
    yaml_text = yaml.safe_dump(
        raw, sort_keys=False, default_flow_style=False
    ).rstrip("\n")
    if prose:
        return f"---\n{yaml_text}\n---\n\n{prose}\n"
    return f"---\n{yaml_text}\n---\n"


def body_to_task(body: str) -> tuple[TaskRecord, str]:
    """Parse an existing issue body back into `(TaskRecord, prose)`.

    Raises `ValueError` listing intake errors if the body doesn't
    validate. This is a convenience for maintainer scripts that read
    issues created earlier (their own or a contributor's) and want
    the typed record back out.
    """
    result = parse_issue_body(body)
    if isinstance(result, ParseSuccess):
        return result.record, result.body_prose
    errs: list[ParseError] = result
    summary = "; ".join(f"{e.code}: {e.message}" for e in errs)
    raise ValueError(f"issue body failed intake parse — {summary}")
