"""Keep unclaimed tasks pinned to the tree their workers will clone.

A task's `project_ref.commit` is the tree its worker checks out. A merge
that touches the task's `target_file` moves that text, so the pin names a
tree the branch no longer has and the next worker starts from stale
source. The merge that caused the drift is also the event that can repair
it: it knows which files it touched, so no history has to be walked to
find the affected tasks. `issue-close-on-merge` runs this.

Only tasks still labelled `choir/available` are repinned, and that label is
what the merge can see. A claim is a comment (`gate/state/lease_arbiter.py`)
and the label follows it only at the orchestrator's next lease sync, so a
task claimed within that window is still repinned. Nothing reads
`project_ref.commit` after the clone — no check compares a PR's base against
it — so the cost is an issue that displays a commit its holder did not build
on, not a submission that fails. The window closes on its own.

Claimed tasks are otherwise left alone on purpose. Every check runs on
`refs/pull/N/merge`, so a PR from a stale base is either a conflict the
worker resolves or a clean merge the gate verifies against current main. A
current pin saves a worker from starting on text that moved; it is not what
makes the verdict sound.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from orchestrator.tasks.api import (
    LABEL_AVAILABLE,
    MaintainerError,
    list_choir_tasks,
    set_task_pin,
)


def repin_available(repo: str, commit: str, changed: Iterable[str]) -> dict[str, Any]:
    """Repin open unclaimed tasks whose target file is in `changed`.

    `commit` is the tree to pin to — the merge commit on the default
    branch. Returns the tasks moved, and any whose issue body could not be
    rewritten; a single GitHub failure does not abandon the rest.
    """
    touched = set(changed)
    repinned: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    for handle in list_choir_tasks(repo, state="open", label=LABEL_AVAILABLE):
        record = handle.record
        if record is None or record.target_file not in touched:
            continue
        if record.project_ref.commit == commit:
            continue

        entry = {
            "issue": handle.number,
            "target_file": record.target_file,
            "from": record.project_ref.commit,
            "to": commit,
        }
        try:
            set_task_pin(repo, handle.number, commit)
        except (MaintainerError, ValueError) as e:
            failed.append({**entry, "reason": str(e)})
            continue
        repinned.append(entry)

    return {"commit": commit, "repinned": repinned, "failed": failed}


def main(argv: list[str] | None = None) -> int:
    """Entry point for the workflow step.

    Outcomes are in the JSON, not the exit code: this runs after the merge,
    so there is nothing left for a nonzero exit to prevent.
    """
    p = argparse.ArgumentParser(description="Repin unclaimed tasks after a merge")
    p.add_argument("--repo", required=True, help="owner/name")
    p.add_argument("--commit", required=True, help="the tree to pin to")
    p.add_argument("--files-file", required=True,
                   help="file of repo-relative paths the merge touched, one per line")
    a = p.parse_args(argv)

    changed = [
        line.strip()
        for line in Path(a.files_file).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    print(json.dumps(repin_available(a.repo, a.commit, changed), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
