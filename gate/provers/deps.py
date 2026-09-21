"""Shared dependency-probe runner, sibling of `gate.provers.trust`.

`collect_dependencies` is the one place that knows how to turn a
`ProverProfile`'s `dependency_command`/`parse_dependencies` hooks into
an actual subprocess run: build the probe (the command-builder hook
writes the probe file as a side effect and returns the argv to run it),
execute it in the workspace, and parse the captured output into
`DeclDependency` records.

What the probe reports is the *elaborated* environment, so it answers a
question no text scan can: which of a project's own declarations a
proof term actually reaches. That is what a roadmap's dependency edges
claim, which is why they can be regenerated rather than maintained by
hand.

Two failure modes are kept distinct from a result, because both would
otherwise read as "this project has no dependencies" and a caller
writing that into a plan would erase every edge the plan had:

- a prover with no probe raises `ProverError` rather than returning
  `[]`, and
- a nonzero subprocess exit — a broken build, a missing binary, a
  module that does not import — raises rather than returning whatever
  the probe managed to print first.

The probe needs a *built* project, since it reads compiled artifacts
rather than re-elaborating. Against an unbuilt or half-built checkout
the import fails and the nonzero exit carries the reason.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from gate.provers import ProverError
from gate.provers.base import DeclDependency, ProverProfile


def collect_dependencies(
    profile: ProverProfile,
    workspace: Path,
    imports: list[str],
    watched: list[str] | None = None,
) -> list[DeclDependency]:
    """Run `profile`'s dependency probe over `imports` and parse the result.

    `imports` are the project's own modules — both what the probe
    imports and the set it reports on. Returns `[]` without running
    anything when `imports` is empty; there is nothing to probe, and
    that is the one case where an empty result is the honest answer.

    `watched` are library declarations the probe should report as
    dependencies alongside the project's own. Library constants are
    otherwise dropped, since a proof term reaches hundreds of them and
    an edge to each would bury the plan; a watched name is one the
    roadmap already names, so reporting it adds an edge to a node that
    is in the plan already and cannot enlarge it.

    Raises `gate.provers.ProverError` when the prover has no probe or
    the probe subprocess exits nonzero.

    The probe is written to a scratch directory and run against
    `workspace`, which is left untouched. Nothing this function does can
    add a file to the project, even if the process dies mid-run — the
    reading must not be able to change what it reads.
    """
    if profile.dependency_command is None or profile.parse_dependencies is None:
        raise ProverError(f"{profile.name} has no dependency probe")
    if not imports:
        return []
    with tempfile.TemporaryDirectory() as scratch:
        argv = profile.dependency_command(
            Path(scratch), list(imports), list(watched or [])
        )
        result = subprocess.run(
            argv, cwd=workspace, capture_output=True, text=True, check=False
        )
    if result.returncode != 0:
        # Lean writes compile errors to stdout, not stderr — prefer
        # stderr when present but fall back so the error is never empty.
        detail = result.stderr.strip() or result.stdout.strip()
        raise ProverError(
            f"dependency probe failed (exit {result.returncode}): {detail}"
        )
    return profile.parse_dependencies(result.stdout)
