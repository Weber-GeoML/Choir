"""Shared environment-level trust-report runner (design note 12 §4).

`collect_trust_report` is the one place that knows how to turn a
`ProverProfile`'s `trust_report_command`/`parse_trust_report` hooks
into an actual subprocess run: build the probe (the command-builder
hook writes whatever probe file the prover needs as a side effect and
returns the argv to run it), execute it in the workspace, and parse
the captured output into `TrustEntry` records.

Two probing shapes exist (design note 12 §4):

- Most provers (lean4, isabelle) name each declaration in their probe
  output, so one probe covers every target at once.
- rocq's `Print Assumptions` doesn't echo the queried name — a profile
  sets `one_probe_per_decl = True` to have this runner invoke the
  builder once per target and attribute each single-decl result to
  the decl that produced it (the parser itself returns a placeholder
  `decl=""`; see `gate.provers.rocq.parse_rocq_trust_report`).

A nonzero subprocess exit is always an infrastructure error — a
mis-typed `--decl` name, a broken build, a missing prover binary —
never a trust *finding*, so it raises `ProverError` rather than
folding into the returned entries. The CLI
(`gate/verify/trust_report_cli.py`) is the layer that turns that into
exit code 2 (the environment-level trust report itself is otherwise
purely informational).
"""

from __future__ import annotations

import dataclasses
import subprocess
from pathlib import Path

from gate.provers import ProverError
from gate.provers.base import ProverProfile, TrustEntry


def collect_trust_report(
    profile: ProverProfile,
    workspace: Path,
    targets: list[str],
    imports: list[str] | None = None,
) -> list[TrustEntry]:
    """Run `profile`'s trust-report probe over `targets` and parse the result.

    `imports` are the modules/theories/sessions the probe needs in
    scope (the CLI's repeatable `--import`); passed through to the
    command-builder hook unchanged. Returns `[]` without running
    anything when `targets` is empty — there is nothing to probe.

    Raises `gate.provers.ProverError` if the probe subprocess exits
    nonzero.

    Always cleans up the probe file(s) it writes into `workspace`
    (`.choir-trust-probe.*`) before returning or raising, so a probe
    never lingers for a later `git add -A` to accidentally pick up.
    """
    if not targets:
        return []
    resolved_imports = list(imports) if imports is not None else []

    try:
        if profile.one_probe_per_decl:
            entries: list[TrustEntry] = []
            for decl in targets:
                output = _run_probe(profile, workspace, [decl], resolved_imports)
                parsed = profile.parse_trust_report(output)
                entries.extend(dataclasses.replace(entry, decl=decl) for entry in parsed)
            return entries

        output = _run_probe(profile, workspace, list(targets), resolved_imports)
        return profile.parse_trust_report(output)
    finally:
        for probe_file in workspace.glob(".choir-trust-probe.*"):
            probe_file.unlink(missing_ok=True)


def _run_probe(
    profile: ProverProfile, workspace: Path, targets: list[str], imports: list[str]
) -> str:
    argv = profile.trust_report_command(workspace, targets, imports)
    result = subprocess.run(
        argv,
        cwd=workspace,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        # Lean (empirically, design note 12 §4's live test) writes
        # compile errors to stdout, not stderr — prefer stderr when
        # present but fall back to stdout so the raised error is
        # never empty.
        detail = result.stderr.strip() or result.stdout.strip()
        raise ProverError(
            f"trust-report probe failed (exit {result.returncode}): {detail}"
        )
    return result.stdout
