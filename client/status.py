"""`choir status` — list local workspaces.

v0 is local-only: walks `~/.choir/work/` (or the path under
`$CHOIR_WORK_ROOT`), reads each `.choir-lease.json`, and yields a
summary record. No GitHub API calls — the local view shows what *this
machine* thinks; remote state (still claimed? merged?) is a future
enhancement.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from client.workspace import LeaseMetadata, workspace_root


@dataclass
class WorkspaceEntry:
    repo: str
    issue: int
    branch: str
    target_decl: str
    task_type: str
    claimed_at: str
    workspace_path: Path


def list_workspaces(root: Path | None = None) -> list[WorkspaceEntry]:
    """Return one `WorkspaceEntry` per `.choir-lease.json` under `root`.

    Sorted by `claimed_at` descending (most recently claimed first).
    Silently skips workspaces with unreadable or malformed metadata —
    `choir status` should never crash because one rogue directory has
    a bad JSON file.
    """
    base = root if root is not None else workspace_root()
    if not base.is_dir():
        return []

    entries: list[WorkspaceEntry] = []
    for lease_path in base.rglob(".choir-lease.json"):
        if not lease_path.is_file():
            continue
        try:
            meta = LeaseMetadata.read(lease_path)
        except (ValueError, KeyError, TypeError):
            # Malformed metadata — skip rather than crash the listing.
            continue
        entries.append(
            WorkspaceEntry(
                repo=meta.repo,
                issue=meta.issue,
                branch=meta.branch,
                target_decl=meta.target_decl,
                task_type=meta.task_type,
                claimed_at=meta.claimed_at,
                workspace_path=lease_path.parent,
            )
        )

    entries.sort(key=lambda e: e.claimed_at, reverse=True)
    return entries


def format_table(entries: list[WorkspaceEntry]) -> str:
    """Pretty-print a list of workspace entries as a fixed-column table."""
    if not entries:
        return "No local workspaces.\n"

    header = ["ISSUE", "TYPE", "TARGET", "BRANCH", "CLAIMED", "PATH"]
    rows = [
        [
            f"{e.repo}#{e.issue}",
            e.task_type,
            e.target_decl,
            e.branch,
            e.claimed_at,
            str(e.workspace_path),
        ]
        for e in entries
    ]
    widths = [
        max(len(row[i]) for row in [header, *rows]) for i in range(len(header))
    ]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    lines = [fmt.format(*header), fmt.format(*("-" * w for w in widths))]
    lines.extend(fmt.format(*row) for row in rows)
    return "\n".join(lines) + "\n"
