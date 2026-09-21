"""CLI: coherence indexer audit on a PR.

Invoked by `.github/workflows/verify-coherence.yml`. Scans both the
base and head versions of all `.lean` files in the repo, finds newly-
introduced declarations in head, and checks for last-segment name
collisions with existing declarations elsewhere in the project.

Exit codes:
    0 = always. The audit is informational — output goes to the
        workflow log (and optionally to a PR comment in a future
        slice). It never blocks merge.

Inputs:
    --base-dir : path to a checkout at the PR base SHA
    --head-dir : path to a checkout at the PR head SHA

The workflow checks out at the head, then re-clones into a temp dir
at the base, then invokes this CLI with both paths.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from gate.indexer.check import (
    find_collisions,
    find_introductions,
    format_findings,
)
from gate.indexer.extract import scan_directory
from gate.provers import ProverError, get_profile
from gate.provers.select import read_prover


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Coherence indexer (name-collision check)"
    )
    parser.add_argument("--base-dir", required=True, help="Project root at PR base")
    parser.add_argument("--head-dir", required=True, help="Project root at PR head")
    parser.add_argument(
        "--prover",
        default=None,
        help=(
            "Override the prover profile (lean4/isabelle/rocq). Defaults to "
            "--base-dir's '.choir/project.toml' [project] prover key — the "
            "base-SHA checkout, same discipline as the PR-triggered audits."
        ),
    )
    args = parser.parse_args(argv)

    base_root = Path(args.base_dir)
    head_root = Path(args.head_dir)

    if not base_root.is_dir():
        print(f"error: --base-dir {base_root} is not a directory", file=sys.stderr)
        return 1
    if not head_root.is_dir():
        print(f"error: --head-dir {head_root} is not a directory", file=sys.stderr)
        return 1

    try:
        prover_name = args.prover if args.prover is not None else read_prover(base_root)
        profile = get_profile(prover_name)
    except ProverError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    print("=== coherence indexer ===")
    print(f"base: {base_root}")
    print(f"head: {head_root}")
    print()

    base_decls = scan_directory(base_root, profile=profile)
    head_decls = scan_directory(head_root, profile=profile)
    print(f"declarations in base: {len(base_decls)}")
    print(f"declarations in head: {len(head_decls)}")

    introductions = find_introductions(base_decls, head_decls)
    print(f"new declarations in head: {len(introductions)}")
    print()

    findings = find_collisions(introductions, base_decls)
    print(format_findings(findings))
    return 0


if __name__ == "__main__":
    sys.exit(main())
