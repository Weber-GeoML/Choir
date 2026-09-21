"""CLI: `python -m gate.inventory scan <path> [--format json|md]`.

Emits the trust-boundary inventory of a local Lean checkout. The
orchestrator agent runs this after cloning / pulling the project to
decide what tasks to publish next and to report the trust boundary
to the overseer.

Exit codes:
    0 — scan succeeded (output on stdout; empty inventory is success)
    1 — invalid arguments / path
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from gate.inventory.scan import TrustBoundary, scan_tree
from gate.provers import ProverError, get_profile
from gate.provers.select import read_prover


def _emit_md(tb: TrustBoundary, stream) -> None:  # type: ignore[no-untyped-def]
    s = tb.as_dict()["summary"]
    stream.write(
        f"# Trust boundary\n\n"
        f"{s['axiom_count']} axiom(s), {s['sorry_count']} sorry(ies) "  # type: ignore[index]
        f"across {s['files_scanned']} file(s).\n"  # type: ignore[index]
    )
    if tb.axioms:
        stream.write("\n## Axioms\n\n| name | location |\n|---|---|\n")
        for a in tb.axioms:
            stream.write(f"| `{a.name}` | {a.file}:{a.line} |\n")
    if tb.sorries:
        stream.write("\n## Sorries\n\n| declaration | location |\n|---|---|\n")
        for so in tb.sorries:
            decl = f"`{so.decl}`" if so.decl else "(no enclosing decl found)"
            stream.write(f"| {decl} | {so.file}:{so.line} |\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="gate.inventory",
        description="Inventory the axioms and sorries in a Lean tree.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan", help="scan a checkout and emit the inventory")
    s.add_argument("path", help="root of the Lean project checkout")
    s.add_argument(
        "--format",
        choices=["json", "md"],
        default="json",
        help="output format (default: json)",
    )
    s.add_argument(
        "--prover",
        default=None,
        help=(
            "Override the prover profile (lean4/isabelle/rocq). Defaults to "
            "the checkout's '.choir/project.toml' [project] prover key."
        ),
    )
    args = parser.parse_args(argv)

    root = Path(args.path)
    if not root.is_dir():
        print(f"error: not a directory: {root}", file=sys.stderr)
        return 1

    try:
        prover_name = args.prover if args.prover is not None else read_prover(root)
        profile = get_profile(prover_name)
    except ProverError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    boundary = scan_tree(root, profile=profile)
    if args.format == "md":
        _emit_md(boundary, sys.stdout)
    else:
        json.dump(boundary.as_dict(), sys.stdout, indent=2)
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
