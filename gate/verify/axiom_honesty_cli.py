"""CLI: axiom-honesty (trust-pattern) audit on a PR.

Invoked by `.github/workflows/verify-axiom-honesty.yml`. Reads the PR's
changed files via the shared `gate.verify.pr_files.fetch_pr_files`
(paginated past `gh pr view --json files`'s 100-file cap), fetches base
and head versions of each via `git show`, and runs
`axiom_honesty.compare`. Aggregates findings across all changed files.
The check name (`verify-axiom-honesty`) stays fixed across provers;
which constructs count as trust-eroding comes from the effective
`ProverProfile` (`--prover` flag > base-SHA `.choir/project.toml` >
lean4 default).

Exit codes:
    0 = audit passes (no new trust-eroding constructs in any changed file)
    1 = audit fails (new constructs introduced; human review required)
    2 = environmental failure (gh/git error, or an unresolvable prover)
"""

from __future__ import annotations

import argparse
import subprocess
import sys

from gate.provers import ProverError
from gate.verify.axiom_honesty import (
    Finding,
    Verdict,
    compare,
    format_findings,
)
from gate.verify.config import VerifyConfig, load_verify_config_at_sha
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
    try:
        return _git("show", f"{sha}:{path}")
    except _ExternalError:
        return ""


def load_verify_config_from_base(base_sha: str) -> VerifyConfig:
    """Read `.choir/verify.toml` from the base SHA, not the PR head.

    A contributor could otherwise weaken the policy in their own PR
    (e.g., add an axiom name to `allowed_axioms`). Reading from base
    means the policy lives on the protected branch — only a maintainer
    can change it. Defaults if absent or malformed (fail safe strict).
    """
    return load_verify_config_at_sha(base_sha)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Axiom-honesty audit")
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
        # _ExternalError (this module's own git/gh-failure type, used
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
        # infrastructure failure, not "the contributor introduced an
        # axiom" — must not fall through to Python's default exit 1,
        # which the exit contract reserves for the latter.
        print(f"error: {e}", file=sys.stderr)
        return 2
    files = filter_files_by_profile(all_files, profile)

    config = load_verify_config_from_base(base_sha)

    print("=== axiom-honesty audit ===")
    print(f"prover: {profile.name}")
    print(f"base: {base_sha}")
    print(f"head: {head_sha}")
    print(f"policy: {config.axiom_honesty.policy.value}")
    if config.axiom_honesty.allowed_axioms:
        print(
            f"allowed axioms: {', '.join(config.axiom_honesty.allowed_axioms)}"
        )
    print(f"changed {'/'.join(profile.file_extensions)} files: {len(files)}")
    print()

    if not files:
        print("No matching source files changed; audit not applicable.")
        return 0

    any_introduced = False
    for path in files:
        base = _show(base_sha, path)
        head = _show(head_sha, path)
        verdict, findings = compare(
            base, head, config=config.axiom_honesty, profile=profile
        )
        if verdict == Verdict.INTRODUCED:
            any_introduced = True
            print(f"⚠ {path}: new axiomatic constructs")
            print(format_findings(findings))
            print()
        else:
            print(f"✓ {path}: clean")

    if any_introduced:
        print()
        print(
            "PR introduces axiomatic constructs not present in the base. "
            "Per AGENTS.md non-negotiable: these block merge until "
            "human-reviewed."
        )
        return 1
    return 0


# Re-export Finding for callers that want it; keeps the public surface
# of the CLI module discoverable without two imports.
__all__ = ["Finding", "main"]


if __name__ == "__main__":
    sys.exit(main())
