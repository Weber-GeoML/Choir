"""CLI: style audit on a PR.

Mirrors the shape of `axiom_honesty_cli.py` and `decide_instance_cli.py`.
Reads the PR's changed files via the shared
`gate.verify.pr_files.fetch_pr_files` (paginated past `gh pr view
--json files`'s 100-file cap). Aggregates findings across all changed
source files (per the effective prover's `file_extensions` —
`--prover` flag > base-SHA `.choir/project.toml` > lean4 default);
threshold is configurable via `--threshold-lines` (default 200, or
`.choir/verify.toml` at the base SHA).

Exit codes:
    0 = audit passes (no new long declarations) or reports them as advisory
    2 = environmental failure (gh / git error, or an unresolvable prover)
"""

from __future__ import annotations

import argparse
import contextlib
import subprocess
import sys

from gate.provers import ProverError
from gate.verify.config import load_verify_config_at_sha
from gate.verify.pr_files import PrFilesError, fetch_pr_files
from gate.verify.prover_dispatch import filter_files_by_profile, resolve_prover_profile
from gate.verify.style import (
    Verdict,
    compare,
    format_findings,
)


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
    with contextlib.suppress(_ExternalError):
        return _git("show", f"{sha}:{path}")
    return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Style audit (line length)")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument(
        "--threshold-lines",
        type=int,
        default=None,
        help="Max lines per declaration. Default: read "
        "[audits.style].threshold_lines from .choir/verify.toml at the base "
        "SHA, else 200.",
    )
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
        # environmental failure per this CLI's own exit-code table
        # above — must not fall through to Python's default exit 1,
        # which this check never otherwise returns.
        print(f"error: {e}", file=sys.stderr)
        return 2
    files = filter_files_by_profile(all_files, profile)

    # Threshold precedence: explicit flag > base-SHA project config > default.
    # Reading from the base SHA (like the other audits) means a PR can't
    # loosen the style bound in its own diff.
    if args.threshold_lines is not None:
        threshold = args.threshold_lines
    else:
        threshold = load_verify_config_at_sha(base_sha).style.threshold_lines

    print("=== style audit (line length) ===")
    print(f"prover:    {profile.name}")
    print(f"base:      {base_sha}")
    print(f"head:      {head_sha}")
    print(f"threshold: {threshold} lines per declaration")
    print(f"changed {'/'.join(profile.file_extensions)} files: {len(files)}")
    print()

    if not files:
        print("No matching source files changed; audit not applicable.")
        return 0

    any_long = False
    for path in files:
        base = _show(base_sha, path)
        head = _show(head_sha, path)
        verdict, findings = compare(
            base, head, threshold=threshold, profile=profile
        )
        if verdict == Verdict.INTRODUCED:
            any_long = True
            print(f"⚠ {path}: new declarations exceed length threshold")
            print(format_findings(findings))
            print()
        else:
            print(f"✓ {path}: clean")

    if any_long:
        print()
        print(
            "\nThese declarations exceed the project's length threshold. "
            "This check is ADVISORY (spec 2026-08-17 D9): length is a soft "
            "signal for the orchestrator's review, not a merge gate — an "
            "honest proof may legitimately be long. Adjust "
            "[audits.style] threshold_lines in .choir/verify.toml if the "
            "default does not fit this project."
        )
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
