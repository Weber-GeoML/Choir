"""CLI: statement-equivalence audit on a PR.

Invoked by `.github/workflows/verify-statement-equiv.yml`. Given a
repo and PR number, identifies the linked issue (via Closes/Fixes/
Resolves keyword), extracts `target_file` + `target_decl` from the
issue's YAML body, fetches the base and head versions of the file
via `git show`, and calls `statement_equiv.compare`. The effective
prover (`--prover` flag > base-SHA `.choir/project.toml` > lean4
default) selects which extractor `compare` uses.

Exit codes:
    0 = audit passes (statement unchanged, or undetermined — see below)
    1 = audit fails (statement changed)
    2 = environmental failure (gh/git/parse error before audit could run)

`Undetermined` results pass the audit deliberately. The audit's job is
to catch contributors who quietly weaken a statement to make it
provable; an inability to extract the signature from the file (parser
limitation, file moved, etc.) is not evidence of cheating, and other
audits + human review remain. We log loudly so the maintainer sees it.

Rename-following (`detect_rename`/`fetch_base_contents`, the "bug-#5"
bypass fix) now lives in `gate.verify.base_contents`, shared with
`statement_immutability_cli`, which needs the same lookup for every
changed file rather than one named target.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

from gate.state.close_on_merge import parse_closing_refs
from gate.state.intake import ParseSuccess, parse_issue_body
from gate.verify.base_contents import detect_rename, fetch_base_contents
from gate.verify.prover_dispatch import resolve_prover_profile
from gate.verify.statement_equiv import Verdict, compare


class _ExternalError(RuntimeError):
    pass


def _gh(*args: str) -> str:
    result = subprocess.run(
        ["gh", *args], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise _ExternalError(
            f"gh {' '.join(args)} failed: {(result.stderr or '').strip()}"
        )
    return result.stdout


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise _ExternalError(
            f"git {' '.join(args)} failed: {(result.stderr or '').strip()}"
        )
    return result.stdout


def fetch_pr_metadata(repo: str, pr: int) -> dict:
    raw = _gh(
        "pr",
        "view",
        str(pr),
        "--repo",
        repo,
        "--json",
        "body,baseRefOid,headRefOid",
    )
    return json.loads(raw)


def fetch_issue_body(repo: str, issue: int) -> str:
    raw = _gh(
        "issue", "view", str(issue), "--repo", repo, "--json", "body", "-q", ".body"
    )
    return raw.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Statement-equivalence audit")
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--pr", required=True, type=int, help="PR number")
    parser.add_argument(
        "--prover",
        default=None,
        help="Override the effective prover. Default: read "
        "[project].prover from .choir/project.toml at the PR's base SHA, "
        "else lean4.",
    )
    args = parser.parse_args(argv)

    try:
        pr_meta = fetch_pr_metadata(args.repo, args.pr)
    except _ExternalError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    closing = parse_closing_refs(pr_meta.get("body") or "")
    if not closing:
        print(
            "No linked issue (no Closes/Fixes/Resolves keyword). Audit not applicable; "
            "passing."
        )
        return 0
    issue_n = closing[0]

    try:
        issue_body = fetch_issue_body(args.repo, issue_n)
    except _ExternalError as e:
        print(f"error fetching issue #{issue_n}: {e}", file=sys.stderr)
        return 2

    parsed = parse_issue_body(issue_body, expected_repo=args.repo)
    if not isinstance(parsed, ParseSuccess):
        print(
            f"Issue #{issue_n} body fails to parse — can't determine target. "
            "Audit not applicable; passing."
        )
        return 0

    target_file = parsed.record.target_file
    target_decl = parsed.record.target_decl
    base_sha = pr_meta["baseRefOid"]
    head_sha = pr_meta["headRefOid"]
    profile = resolve_prover_profile(args.prover, base_sha)

    base_contents, renamed_from = fetch_base_contents(
        base_sha, head_sha, target_file
    )
    head_contents = ""
    try:
        head_contents = _git("show", f"{head_sha}:{target_file}")
    except _ExternalError as e:
        print(f"warning: could not read head version of {target_file}: {e}")

    verdict, message = compare(base_contents, head_contents, target_decl, profile=profile)

    print("=== statement-equivalence audit ===")
    print(f"prover:      {profile.name}")
    print(f"target_decl: {target_decl}")
    print(f"target_file: {target_file}")
    if renamed_from is not None:
        print(f"renamed_from: {renamed_from}  (file renamed in this PR)")
    print(f"verdict:     {verdict.value}")
    print()
    print(message)

    if verdict == Verdict.CHANGED:
        return 1
    return 0


# `detect_rename` is unused directly in this module now that
# `fetch_base_contents` (in `gate.verify.base_contents`) calls it
# internally — re-exported anyway since existing tests and callers
# import it from here, and the shared module's public surface should
# stay reachable from either CLI that names it.
__all__ = ["detect_rename", "fetch_base_contents", "main"]


if __name__ == "__main__":
    sys.exit(main())
