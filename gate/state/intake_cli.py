"""CLI entry point for issue-intake.

Called by `.github/workflows/issue-intake.yml`. Reads the issue body from
a file (written by the workflow with `printf` from an env var, to avoid
shell-quoting hazards), validates it via
`gate.state.intake.parse_issue_body`, and emits a JSON payload to
stdout describing what the workflow should do next: post a comment, add
labels, remove labels.

The CLI itself does no GitHub API calls — that's the workflow's job. This
keeps the CLI unit-testable without mocking `gh` and matches the AGENTS.md
"workflows are thin" pattern.

Output JSON shape:
    {
      "status": "ok" | "error",
      "comment": "<markdown to post on the issue>",
      "should_post_comment": true | false,
      "add_labels": ["choir/available", ...],
      "remove_labels": ["choir/invalid", ...],
      "errors": [ {code, field, message, hint}, ... ]   # only on error
    }

Idempotency: when the workflow passes `--existing-comments-file`, the
CLI parses prior intake-ack comments for `choir-intake-ack:<hash>`
markers. If the marker for the current canonical record already
exists, `should_post_comment` is set to false. Label updates still
apply (they're idempotent on GitHub's side). This stops `issues.edited`
events from spamming the issue thread when only the prose part of
the body changed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from gate.provers import get_profile
from gate.provers.select import read_prover
from gate.state.intake import ParseError, ParseSuccess, parse_issue_body
from gate.state.task_record import TaskRecord

_HASH_LEN = 16  # 16 hex chars of SHA-256; plenty for collision-resistance per issue
_HASH_RE = re.compile(r"choir-intake-ack:([a-f0-9]+)")


def record_hash(record: TaskRecord) -> str:
    """Stable hash of the canonical record.

    Any field change (target_decl, commit, deps order, blueprint_ref, ...)
    produces a different hash. Used as the marker for intake-comment
    idempotency.
    """
    canonical = {
        "version": record.choir_task_version,
        "type": record.type.value,
        "target_file": record.target_file,
        "target_decl": record.target_decl,
        "project_ref": {
            "repo": record.project_ref.repo,
            "commit": record.project_ref.commit,
            "toolchain": record.project_ref.toolchain,
        },
        "deps": list(record.deps),  # order matters
        "blueprint_ref": record.blueprint_ref,
    }
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:_HASH_LEN]


def existing_hashes(existing_comments: list[str]) -> set[str]:
    """Find all `choir-intake-ack:<hash>` markers in the given comment bodies."""
    out: set[str] = set()
    for comment in existing_comments:
        out.update(_HASH_RE.findall(comment))
    return out


def render_success_comment(success: ParseSuccess) -> str:
    record = success.record
    deps_str = ", ".join(f"#{n}" for n in record.deps) if record.deps else "_(none)_"
    blueprint = f"`{record.blueprint_ref}`" if record.blueprint_ref else "_(none)_"
    h = record_hash(record)
    target_rows = (
        f"| target_file | `{record.target_file}` |\n"
        f"| target_decl | `{record.target_decl}` |\n"
    )
    return (
        f"<!-- choir-intake-ack:{h} -->\n"
        "**Choir intake**: task accepted ✓\n\n"
        "| field | value |\n"
        "|---|---|\n"
        f"| type | `{record.type.value}` |\n"
        f"{target_rows}"
        f"| project | `{record.project_ref.repo}@{record.project_ref.commit}` |\n"
        f"| toolchain | `{record.project_ref.toolchain}` |\n"
        f"| blueprint_ref | {blueprint} |\n"
        f"| deps | {deps_str} |\n"
        "\nThis issue is now available for claiming.\n"
    )


def render_error_comment(errors: list[ParseError]) -> str:
    rows = "\n".join(
        f"| `{e.code}` | `{e.field or '_'}` | {e.message} |" for e in errors
    )
    return (
        "**Choir intake**: task **rejected** — fix the issues below and edit "
        "the issue body to retry.\n\n"
        "| code | field | message |\n"
        "|---|---|---|\n"
        f"{rows}\n"
        "\nThe issue template at `.github/ISSUE_TEMPLATE/choir-task.md` "
        "shows the expected shape.\n"
    )


def build_payload(
    body: str,
    *,
    repo: str,
    existing_comments: list[str] | None = None,
    allowed_extensions: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Validate the body and produce the workflow's action payload.

    If `existing_comments` is provided, the payload's `should_post_comment`
    is false when the same canonical-record hash already appears in a
    prior comment — implementing intake idempotency on `issues.edited`.

    `allowed_extensions` is the selected prover profile's
    `file_extensions` (see `gate.provers`); `None` skips the check
    (library back-compat — see `parse_issue_body`).
    """
    result = parse_issue_body(body, expected_repo=repo, allowed_extensions=allowed_extensions)
    prior_hashes = existing_hashes(existing_comments) if existing_comments else set()

    if isinstance(result, ParseSuccess):
        h = record_hash(result.record)
        return {
            "status": "ok",
            "comment": render_success_comment(result),
            "should_post_comment": h not in prior_hashes,
            "record_hash": h,
            "add_labels": [
                "choir/task",
                f"choir/type:{result.record.type.value}",
                "choir/available",
            ],
            "remove_labels": ["choir/invalid"],
        }
    # On error, always surface the latest comment — even if the maintainer
    # edited and produced the same error again, they need to see it.
    return {
        "status": "error",
        "comment": render_error_comment(result),
        "should_post_comment": True,
        "add_labels": ["choir/task", "choir/invalid"],
        "remove_labels": ["choir/available"],
        "errors": [asdict(e) for e in result],
    }


def _load_existing_comments(path: str | None) -> list[str] | None:
    if path is None:
        return None
    raw = Path(path).read_text(encoding="utf-8")
    # The workflow writes the result of `gh issue view --json comments
    # --jq '.comments[].body'` — one body per line. Newlines inside a
    # comment body get escaped to `\n` by jq, which is fine: the
    # marker regex finds them regardless.
    return [line for line in raw.splitlines() if line.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Choir issue intake")
    parser.add_argument(
        "--body-file",
        required=True,
        help="Path to a file containing the issue body (Markdown)",
    )
    parser.add_argument(
        "--repo",
        required=True,
        help="GitHub repo this issue lives in, as 'owner/name'",
    )
    parser.add_argument(
        "--existing-comments-file",
        default=None,
        help=(
            "Optional path to a file containing prior issue comments "
            "(one body per line). Used for intake-comment idempotency."
        ),
    )
    parser.add_argument(
        "--prover",
        default=None,
        help=(
            "Override the prover profile (lean4/isabelle/rocq). Defaults to "
            "the checkout's '.choir/project.toml' [project] prover key "
            "(design note 12 §5) — intake has no base SHA to read from, so "
            "this reads the workspace at cwd, which the workflow checks out "
            "at the default branch."
        ),
    )
    args = parser.parse_args(argv)

    body = Path(args.body_file).read_text(encoding="utf-8")
    existing = _load_existing_comments(args.existing_comments_file)
    prover_name = args.prover if args.prover is not None else read_prover(Path.cwd())
    profile = get_profile(prover_name)
    payload = build_payload(
        body,
        repo=args.repo,
        existing_comments=existing,
        allowed_extensions=profile.file_extensions,
    )
    json.dump(payload, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
