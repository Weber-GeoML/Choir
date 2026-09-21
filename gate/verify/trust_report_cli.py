"""CLI: environment-level trust report for a set of declarations.

Design note 12 §4 — this is the "environment-level trust report"
check: after a rebuild, ask the *built environment* (not text scanning)
what a declaration's trust boundary actually is. Informational only:
this check never blocks merge, so it always exits 0 on a successful
probe run — whether or not any declaration turns out to depend on
axioms/oracles. Exit 2 is reserved for infrastructure errors: a bad
`--workspace`, an unresolvable/unknown prover, or the probe subprocess
itself failing to run or compile.

Unlike the delta audits (`axiom_honesty_cli`, `sorry_delta_cli`, ...),
this CLI has no PR/base-SHA context — it runs against a plain
workspace checkout (a PR head, a worker's clone, or an overseer's
local project). Prover resolution is therefore the same pattern as
`gate/state/intake_cli.py`'s default-branch read: `--prover` flag,
else `[project].prover` from the *workspace's own* `.choir/project.toml`
(default lean4).

Two modes (design note 12 §4.1):

- **Explicit** (`--decl`, repeatable fully-qualified names, plus
  `--import`, repeatable modules/theories/sessions the probe needs in
  scope): unchanged since v0, byte-compatible with existing callers.
  With no `--decl` given at all, the run is a documented no-op.
- **Auto** (`--base-sha <sha>`, mutually exclusive with `--decl`):
  `gate.verify.changed_decls.detect_changed_decls` diffs `base_sha`
  against the workspace's current checkout for changed/new
  declarations (capped at 50, deterministically ordered), then probes
  each one individually via `collect_trust_report`. A `ProverError` on
  one declaration becomes an `unresolved` report line instead of
  failing the run — an informational check must never die on one
  unresolvable name. This is what
  `.github/workflows/verify-trust-report.yml` invokes.

Exit 0 in every reporting outcome (clean, dirty, unresolved, empty
detection, cap truncation) — 2 stays reserved for infrastructure
errors: a bad `--workspace`, an unresolvable/unknown prover, a
git-plumbing failure in detection, or the probe subprocess itself
failing to run or compile.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from gate.provers import ProverError, ProverProfile, get_profile
from gate.provers.select import read_prover
from gate.provers.trust import collect_trust_report
from gate.verify.changed_decls import DEFAULT_CAP, ChangedDeclsError, detect_changed_decls


def _run_auto_mode(
    profile: ProverProfile, workspace: Path, base_sha: str, *, cap: int = DEFAULT_CAP
) -> int:
    """`--base-sha` mode: detect changed decls, then probe each one.

    `cap` is passed straight through to `detect_changed_decls` and reused
    verbatim in the truncation note below, so the message can never drift
    from the value that was actually applied (there's no `--cap` CLI flag
    yet, so `cap` is always `DEFAULT_CAP` today — but the wiring no longer
    depends on that coincidence).
    """
    print(f"base-sha:  {base_sha}")
    print()

    try:
        targets, capped = detect_changed_decls(workspace, base_sha, profile, cap=cap)
    except ChangedDeclsError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if not targets:
        print("No changed declarations detected; nothing to probe.")
        return 0

    print(f"changed declarations detected: {len(targets)}")
    print()

    unresolved: list[tuple[str, str]] = []
    for target in targets:
        try:
            entries = collect_trust_report(
                profile, workspace, [target.name], [target.module]
            )
        except ProverError as e:
            unresolved.append((target.name, str(e)))
            continue
        for entry in entries:
            if entry.clean:
                print(f"✓ {entry.decl}: closed (no axioms/oracles)")
            else:
                print(f"⚠ {entry.decl}: depends on {', '.join(entry.assumptions)}")

    if unresolved:
        print()
        print("unresolved (probe could not be completed for these declarations):")
        for name, detail in unresolved:
            # The whole probe transcript, not its first line (round 5,
            # F3). Where the prover puts its own error text varies:
            # lean4's compile error is the first stdout line, while
            # isabelle's `ML_process` prints `Loading theory "…"` and a
            # `###` timing line *before* the exception — so a
            # first-line summary reported "Loading theory" as the reason
            # a declaration was unresolved. Continuation lines are
            # indented rather than dropped; there is no prover-generic
            # rule for which line matters.
            head, *rest = detail.splitlines() or [""]
            print(f"  {name}: {head}")
            for line in rest:
                print(f"      {line}")

    if capped:
        print()
        print(
            f"note: capped at {cap} changed declarations this run — "
            "the diff had more; re-run with explicit --decl/--import for the rest."
        )

    print()
    print("Informational only — does not affect merge. See design note 12 §4.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Environment-level trust report (informational; design note 12 §4)"
    )
    parser.add_argument(
        "--workspace", required=True, help="Path to the project checkout to probe"
    )
    parser.add_argument(
        "--prover",
        default=None,
        help="Override the effective prover. Default: read "
        "[project].prover from the workspace's .choir/project.toml, "
        "else lean4.",
    )
    parser.add_argument(
        "--decl",
        action="append",
        default=[],
        dest="decls",
        help="Fully-qualified declaration name to probe (repeatable). "
        "Mutually exclusive with --base-sha.",
    )
    parser.add_argument(
        "--import",
        action="append",
        default=[],
        dest="imports",
        help="Module/theory/session the probe needs in scope (repeatable).",
    )
    parser.add_argument(
        "--base-sha",
        default=None,
        dest="base_sha",
        help="Autodetect changed declarations since this SHA and probe each "
        "one (design note 12 §4.1). Mutually exclusive with --decl.",
    )
    args = parser.parse_args(argv)

    if args.base_sha is not None and args.decls:
        print("error: --base-sha and --decl are mutually exclusive", file=sys.stderr)
        return 2

    workspace = Path(args.workspace)
    if not workspace.is_dir():
        print(f"error: {workspace} is not a directory", file=sys.stderr)
        return 2

    try:
        prover_name = args.prover if args.prover is not None else read_prover(workspace)
        profile = get_profile(prover_name)
    except ProverError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    print("=== trust report (informational — design note 12 §4) ===")
    print(f"prover:    {profile.name}")
    print(f"workspace: {workspace}")

    if args.base_sha is not None:
        return _run_auto_mode(profile, workspace, args.base_sha)

    print(f"decls:     {len(args.decls)}")
    print()

    if not args.decls:
        print(
            "No --decl targets specified; informational check, nothing to "
            "probe. Pass --base-sha to autodetect changed declarations "
            "(design note 12 §4.1), or invoke this CLI directly with "
            "explicit --decl/--import args for an ad-hoc/deeper query."
        )
        return 0

    try:
        entries = collect_trust_report(profile, workspace, args.decls, args.imports)
    except ProverError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    for entry in entries:
        if entry.clean:
            print(f"✓ {entry.decl}: closed (no axioms/oracles)")
        else:
            print(f"⚠ {entry.decl}: depends on {', '.join(entry.assumptions)}")

    probed = {entry.decl for entry in entries}
    missing = [decl for decl in args.decls if decl not in probed]
    if missing:
        print()
        print(
            f"note: no trust-report line for: {', '.join(missing)} "
            "(the probe may not have reached them)"
        )

    print()
    print(
        "Informational only — does not affect merge. See design note "
        "12 §4."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
