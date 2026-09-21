"""Reader for `.choir/project.toml` — project-level automation settings.

The overseer sets the automation level once, at project start (2026-06-11
decision). The orchestrator agent reads it at loop start and behaves
accordingly:

- ``auto`` (default): the orchestrator reviews and merges green PRs on
  its own.
- ``approve``: the orchestrator reviews and posts a recommendation;
  a human confirms the merge.
- ``manual``: the orchestrator observes and reports only; a human does
  the reviewing and merging.

The ``[automation]`` section is read by the *orchestrator*, not the
gate. The gate's deterministic audits and branch protection apply
identically at every level — automation level changes who presses the
button, never what must be true before the button works. (The same
file also carries a ``[reconcile]`` section read by the gate's daily
reconcile workflow — see ``gate/reconcile/config.py``. Each reader
takes only its own section.)

Format::

    [automation]
    merge = "auto"   # auto | approve | manual

Absent file or absent key means ``auto`` — automation is the default;
overseers opt *down* to approve/manual, not up.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

PROJECT_CONFIG_RELATIVE_PATH = Path(".choir") / "project.toml"


class MergeAutomation(Enum):
    """Who presses the merge button, from `.choir/project.toml` `[automation] merge`.

    The gate's audits bind at every level; only the actor changes.
    """

    AUTO = "auto"
    APPROVE = "approve"
    MANUAL = "manual"


@dataclass(frozen=True)
class ProjectConfig:
    merge_automation: MergeAutomation = MergeAutomation.AUTO


class ProjectConfigError(RuntimeError):
    """Loading or validating `.choir/project.toml` failed."""


def parse_project_config(text: str) -> ProjectConfig:
    """Parse `.choir/project.toml` text.

    Empty text / missing sections return defaults. Unknown sections and
    keys are ignored (forward-compat). An invalid ``merge`` value raises
    `ProjectConfigError` — a typo'd automation level must not silently
    become ``auto``.
    """
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ProjectConfigError(f"malformed TOML — {e}") from e

    automation = data.get("automation", {}) or {}
    if not isinstance(automation, dict):
        raise ProjectConfigError("'automation' section must be a table")

    merge_raw = automation.get("merge", MergeAutomation.AUTO.value)
    try:
        merge = MergeAutomation(merge_raw)
    except ValueError as e:
        valid = ", ".join(m.value for m in MergeAutomation)
        raise ProjectConfigError(
            f"invalid automation.merge = {merge_raw!r}; valid: {valid}"
        ) from e

    return ProjectConfig(merge_automation=merge)


def read_project_config(workspace: Path) -> ProjectConfig:
    """Read `.choir/project.toml` from a checkout root. Defaults if absent."""
    path = workspace / PROJECT_CONFIG_RELATIVE_PATH
    if not path.is_file():
        return ProjectConfig()
    return parse_project_config(path.read_text(encoding="utf-8"))
