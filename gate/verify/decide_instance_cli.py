"""CLI: decide-instance audit on a PR.

Mirrors the shape of `axiom_honesty_cli.py`. Reads the PR's changed
files via the shared `gate.verify.pr_files.fetch_pr_files` (paginated
past `gh pr view --json files`'s 100-file cap), narrowed to `.lean`
files once the effective prover is confirmed to be lean4, fetches base
+ head versions via `git show`, runs `decide_instance.compare` per
file, aggregates findings.

`decide_instance` is a lean4-only `extra_audit` (design note 12 §2.2/
§3.1 — `Decidable` instances and `Classical.*` usage are Lean/Mathlib
idioms with no analog in isabelle/rocq). For any other effective
prover (`--prover` flag > base-SHA `.choir/project.toml` > lean4
default), this CLI prints a not-applicable line and exits 0 without
attempting to scan.

Exit codes:
    0 = audit passes (no new decidability / classical introductions, or
        not applicable for the effective prover)
    1 = audit fails (introductions found; human review required)
    2 = environmental failure (gh / git error, or an unresolvable prover)
"""

from __future__ import annotations

import argparse
import contextlib
import subprocess
import sys

from gate.provers import ProverError
from gate.verify.decide_instance import (
    Verdict,
    compare,
    format_findings,
)
from gate.verify.pr_files import PrFilesError, fetch_pr_files
from gate.verify.prover_dispatch import filter_files_by_profile, resolve_prover_profile


class _ExternalError(RuntimeError):
    pass


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise _ExternalError(
            f"git {' '.join(args)} failed: {(result.stderr or '').strip()}"
        )
    return result.stdout


def _show(sha: str, path: str) -> str:
    """Return the contents of `path` at `sha`, or '' if not present."""
    with contextlib.suppress(_ExternalError):
        return _git("show", f"{sha}:{path}")
    return ""


def not_applicable_message(prover_name: str) -> str:
    """The exact line printed (and the audit passed) for a non-lean4 prover."""
    return f"decide-instance: not applicable for prover '{prover_name}' (lean4-only audit)"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Decide-instance audit")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument(
        "--prover",
        default=None,
        help="Override the effective prover. Default: read "
        "[project].prover from .choir/project.toml at the PR's base SHA, "
        "else lean4.",
    )
    args = parser.parse_args(argv)

    try:
        base_sha, head_sha, all_files = fetch_pr_files(args.repo, args.pr)
    except (PrFilesError, _ExternalError) as e:
        # PrFilesError is what the shared fetch actually raises;
        # _ExternalError (this module's own git-failure type, used
        # below by `_show`) is caught too so this call site keeps
        # mapping any external-command failure to exit 2, however it's
        # signaled.
        print(f"error: {e}", file=sys.stderr)
        return 2

    try:
        profile = resolve_prover_profile(args.prover, base_sha)
    except ProverError as e:
        # An unresolvable prover (unknown --prover, or malformed
        # [project] prover in the base-SHA .choir/project.toml) is an
        # infrastructure failure, not "the contributor introduced a
        # decidability shortcut" — must not fall through to Python's
        # default exit 1, which the exit contract reserves for the
        # latter.
        print(f"error: {e}", file=sys.stderr)
        return 2
    if profile.name != "lean4":
        print(not_applicable_message(profile.name))
        return 0
    files = filter_files_by_profile(all_files, profile)

    print("=== decide-instance audit ===")
    print(f"base: {base_sha}")
    print(f"head: {head_sha}")
    print(f"changed .lean files: {len(files)}")
    print()

    if not files:
        print("No .lean files changed; audit not applicable.")
        return 0

    any_introduced = False
    for path in files:
        base = _show(base_sha, path)
        head = _show(head_sha, path)
        verdict, findings = compare(base, head)
        if verdict == Verdict.INTRODUCED:
            any_introduced = True
            print(f"⚠ {path}: new decidability / classical introductions")
            print(format_findings(findings))
            print()
        else:
            print(f"✓ {path}: clean")

    if any_introduced:
        print()
        print(
            "PR introduces new Decidable instances or Classical.* usage not "
            "present in the base. Per AGENTS.md non-negotiable: these block "
            "merge until human-reviewed."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
