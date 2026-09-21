"""Version pinning for cross-environment dependencies.

The project's `lean-toolchain` file pins Lean itself (elan reads it
automatically — no Choir code needed). The remaining pinning concern
per AGENTS.md's non-negotiable is the contributor's locally-installed
tooling — primarily `lean4-skills`, which lives outside the project
repo and can drift independently.

`.choir/pins.toml` (optional, in the project repo) declares the
versions the project expects. At workspace-setup time the CLI reads
the pins, queries the contributor's machine for the actual installed
versions, and surfaces any mismatch as a warning. Choir does not
block the claim on a mismatch — the contributor may have a newer or
older version that's functionally compatible, and the verify pipeline
catches actual breakage at PR-build time. The pin check is advisory,
not load-bearing.

Format::

    [pins]
    lean4_skills = "v0.2.3"

    [skill_pack]
    version = "v1.0"

The `[skill_pack]` section is for the project's own skill-pack
versioning (so future Choir tooling can detect when a contributor's
workspace has a skill pack that drifted from what their TaskRecord
was issued against — currently unused, but the field is reserved).
"""

from __future__ import annotations

import subprocess
import tomllib
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

PINS_RELATIVE_PATH = Path(".choir") / "pins.toml"


@dataclass(frozen=True)
class Pins:
    """Versions the project expects in the contributor's environment."""

    lean4_skills: str | None = None
    skill_pack_version: str | None = None


class DetectionStatus(Enum):
    """Why a version wasn't observed (when `observed` is None).

    Captured separately from "matches/doesn't match" so reports can
    distinguish "tool isn't installed at all" from "tool is installed
    but didn't respond" — different user actions.
    """

    NOT_DETECTED = "not_detected"           # detection wasn't attempted (no pin)
    OBSERVED = "observed"                   # version successfully read
    NOT_INSTALLED = "not_installed"         # FileNotFoundError — tool absent
    TIMED_OUT = "timed_out"                 # subprocess.TimeoutExpired
    COMMAND_FAILED = "command_failed"       # tool ran but returned non-zero


@dataclass(frozen=True)
class PinCheck:
    """Result of comparing one pinned version to the observed local version."""

    name: str
    pinned: str
    observed: str | None
    matches: bool  # True if observed == pinned, or if observed is None (no info)
    status: DetectionStatus = DetectionStatus.NOT_DETECTED


def read_pins(workspace: Path) -> Pins | None:
    """Read `.choir/pins.toml` from `workspace`. Return None if absent.

    Malformed TOML is treated as an empty pins file (returns `Pins()`),
    not raised — the pin check is advisory and shouldn't break the
    happy path on a typo'd config.
    """
    path = workspace / PINS_RELATIVE_PATH
    if not path.is_file():
        return None
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError:
        return Pins()

    pins_section = data.get("pins", {}) or {}
    skill_pack_section = data.get("skill_pack", {}) or {}
    return Pins(
        lean4_skills=pins_section.get("lean4_skills"),
        skill_pack_version=skill_pack_section.get("version"),
    )


def detect_lean4_skills_version() -> tuple[str | None, DetectionStatus]:
    """Try to detect the contributor's installed lean4-skills version.

    Returns `(version, status)`:
    - `(version_string, OBSERVED)` on success.
    - `(None, NOT_INSTALLED)` if the binary isn't on PATH.
    - `(None, TIMED_OUT)` if the command hangs past the timeout.
    - `(None, COMMAND_FAILED)` if the binary ran but returned non-zero
      or produced no output.

    Never raises. Callers use the status to produce more specific
    diagnostics than "couldn't detect."
    """
    try:
        result = subprocess.run(
            ["lean4-skills", "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except FileNotFoundError:
        return None, DetectionStatus.NOT_INSTALLED
    except subprocess.TimeoutExpired:
        return None, DetectionStatus.TIMED_OUT
    if result.returncode != 0:
        return None, DetectionStatus.COMMAND_FAILED
    version = result.stdout.strip()
    if not version:
        return None, DetectionStatus.COMMAND_FAILED
    return version, DetectionStatus.OBSERVED


def check_pins(workspace: Path) -> list[PinCheck]:
    """Compare each declared pin against the observed local environment.

    Returns one `PinCheck` per pinned tool. Empty list if no pins file
    exists or no recognized pins are declared.
    """
    pins = read_pins(workspace)
    if pins is None:
        return []

    out: list[PinCheck] = []
    if pins.lean4_skills:
        observed, status = detect_lean4_skills_version()
        out.append(
            PinCheck(
                name="lean4-skills",
                pinned=pins.lean4_skills,
                observed=observed,
                matches=observed is None or observed == pins.lean4_skills,
                status=status,
            )
        )
    # Future: skill_pack_version cross-check once we track which version
    # the TaskRecord was issued against.
    return out


def format_pin_report(checks: list[PinCheck]) -> str:
    """Render `check_pins` results as a short human-readable report."""
    if not checks:
        return ""
    lines: list[str] = []
    for c in checks:
        if c.observed is None:
            # Different diagnostic per detection status. "Not installed"
            # tells the user to install; "timed out" tells them the
            # install may be broken; "command failed" tells them the
            # binary exists but didn't respond to --version.
            if c.status == DetectionStatus.NOT_INSTALLED:
                lines.append(
                    f"  • {c.name} pinned to {c.pinned}; not installed on "
                    "this machine. Install it before claiming tasks."
                )
            elif c.status == DetectionStatus.TIMED_OUT:
                lines.append(
                    f"  • {c.name} pinned to {c.pinned}; --version timed "
                    "out. The install may be broken — try re-installing."
                )
            elif c.status == DetectionStatus.COMMAND_FAILED:
                lines.append(
                    f"  • {c.name} pinned to {c.pinned}; --version "
                    "returned an error. Confirm your version manually."
                )
            else:
                lines.append(
                    f"  • {c.name} pinned to {c.pinned}; couldn't detect "
                    "your installed version — verify manually."
                )
        elif c.matches:
            lines.append(f"  ✓ {c.name} {c.observed} matches pin")
        else:
            lines.append(
                f"  ⚠ {c.name} version mismatch — installed {c.observed}, "
                f"project pins {c.pinned}"
            )
    return "Project version pins (.choir/pins.toml):\n" + "\n".join(lines)
