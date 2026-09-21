"""Priority/difficulty label writers — the orchestrator is the sole writer (design note 11 §4).

Split out of the now-deleted `orchestrator/review_flow.py` (spec D1):
these four functions are not review machinery — they implement the
priority/difficulty label vocabulary that the 2026-07-18 decision made
the sole prioritization tool once publish-timing stopped being one
("publish the full ready frontier"). The struggle-signal difficulty
revision (2026-06-18 stuck-task decision) and the per-loop aging bump
both depend on this module.

Everything judgment-shaped (which tasks to bump, what difficulty a
struggling task deserves) stays in the playbook; this module is
mechanical plumbing, same as its sibling `orchestrator/prs.py`.
"""

from __future__ import annotations

import subprocess

from gate.state.labels import (
    Difficulty,
    Priority,
    difficulty_label,
    escalate,
    parse_priority,
    priority_label,
)
from orchestrator.tasks.api import MaintainerError


def _edit_labels(
    repo: str, number: int, add: list[str], remove: list[str]
) -> None:
    args = ["gh", "issue", "edit", str(number), "--repo", repo]
    for lbl in add:
        args += ["--add-label", lbl]
    for lbl in remove:
        args += ["--remove-label", lbl]
    try:
        subprocess.run(args, capture_output=True, text=True, check=True)
    except FileNotFoundError as e:
        raise MaintainerError("`gh` CLI not found in PATH") from e
    except subprocess.CalledProcessError as e:
        raise MaintainerError(
            f"label edit failed on {repo}#{number}: {(e.stderr or '').strip()}"
        ) from e


def set_priority(repo: str, number: int, priority: Priority) -> None:
    """Set an issue's priority label (orchestrator = sole writer)."""
    keep = priority_label(priority)
    remove = [
        lbl
        for p in Priority
        if (lbl := priority_label(p)) is not None and lbl != keep
    ]
    _edit_labels(repo, number, [keep] if keep else [], remove)


def set_difficulty(repo: str, number: int, difficulty: Difficulty) -> None:
    """Set an issue's difficulty label (orchestrator = sole writer)."""
    keep = difficulty_label(difficulty)
    remove = [difficulty_label(d) for d in Difficulty if d != difficulty]
    _edit_labels(repo, number, [keep], remove)


def bump_priority(
    repo: str, number: int, current_labels: list[str]
) -> Priority:
    """Escalate an aging unclaimed task one tier (note 11 §4). No-op at HIGH."""
    current = parse_priority(current_labels)
    new = escalate(current)
    if new == current:
        return current
    old_lbl = priority_label(current)
    new_lbl = priority_label(new)
    _edit_labels(
        repo, number,
        [new_lbl] if new_lbl else [],
        [old_lbl] if old_lbl else [],
    )
    return new
