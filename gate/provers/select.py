"""Prover selection: read `.choir/project.toml`'s `[project] prover` key.

Mirrors `gate/reconcile/config.py`'s pattern for reading a section of
`.choir/project.toml` gate-side, and `gate/verify/config.py`'s
`load_verify_config_at_sha` `git show` approach for base-SHA reads.

Where each caller reads from (design note 12 §2.3):

- **PR-triggered gate workflows** call `read_prover_at_sha` with the
  PR's *base* SHA — same discipline as the axiom/sorry/style policies,
  so a PR can't switch provers in its own diff to dodge audits.
- **Intake (issues)** and **workers** call `read_prover` against a
  checkout: intake reads the default branch (issues have no base SHA);
  workers read the pinned commit's workspace.

Absent file, absent `[project]` section, or absent `prover` key all
default to `DEFAULT_PROVER` ("lean4") — full back-compat. Unlike
`.choir/verify.toml`'s readers, an explicit-but-unknown value or
malformed TOML raises `ProverError` rather than silently falling back:
prover selection determines *which* audits run at all, so silently
defaulting on a typo would be a worse failure mode than refusing.

Format::

    [project]
    prover = "lean4"   # or "isabelle" / "rocq"
"""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

from gate.provers import PROFILES, ProverError

PROJECT_CONFIG_RELATIVE_PATH = Path(".choir") / "project.toml"
DEFAULT_PROVER = "lean4"


def _validate(name: str) -> str:
    if name not in PROFILES:
        valid = ", ".join(sorted(PROFILES))
        raise ProverError(f"unknown prover {name!r}; valid: {valid}")
    return name


def parse_prover(text: str) -> str:
    """Parse `.choir/project.toml` text, taking only `[project].prover`.

    Empty text / missing section / missing key -> `DEFAULT_PROVER`.
    Unknown sections and keys are ignored (forward-compat — `[automation]`
    and `[reconcile]` live in the same file). An unknown prover value or
    malformed TOML raises `ProverError`.
    """
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ProverError(f"malformed TOML — {e}") from e

    section = data.get("project", {}) or {}
    if not isinstance(section, dict):
        raise ProverError("'project' section must be a table")

    raw = section.get("prover", DEFAULT_PROVER)
    if not isinstance(raw, str):
        raise ProverError(f"project.prover must be a string; got {raw!r}")
    return _validate(raw)


def read_prover(workspace: Path) -> str:
    """Read the selected prover from `.choir/project.toml` under `workspace`.

    Defaults to `DEFAULT_PROVER` if the file is absent.
    """
    path = workspace / PROJECT_CONFIG_RELATIVE_PATH
    if not path.is_file():
        return DEFAULT_PROVER
    return parse_prover(path.read_text(encoding="utf-8"))


def read_prover_at_sha(sha: str, *, cwd: Path | None = None) -> str:
    """Read the selected prover via `git show <sha>:.choir/project.toml`.

    Defaults to `DEFAULT_PROVER` if the file is absent at `sha`.
    """
    result = subprocess.run(
        ["git", "show", f"{sha}:{PROJECT_CONFIG_RELATIVE_PATH}"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout:
        return DEFAULT_PROVER
    return parse_prover(result.stdout)
