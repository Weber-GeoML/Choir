"""Claim-time advisory for unfinished task dependencies.

A TaskRecord's `deps` lists issue numbers this task builds on. This
check warns the contributor at claim time when one of them is still
open — advisory only, like the pins check: the contributor may know the
dep is effectively done (PR open, about to merge) and choose to proceed.

Soft by design (matching the file-scope-soft precedent): Choir blocks
on the verify pipeline, not on planning metadata.
"""

from __future__ import annotations

from client import github as gh


def find_open_deps(repo: str, deps: list[int]) -> list[int]:
    """Return the subset of `deps` that are still open issues.

    Never raises: a dep we can't fetch (deleted issue, network hiccup)
    is skipped — the advisory must not break the claim happy path.
    """
    open_deps: list[int] = []
    for dep in deps:
        try:
            issue = gh.get_issue(repo, dep)
        except gh.GitHubError:
            continue
        if issue.state == "open":
            open_deps.append(dep)
    return open_deps


def format_deps_report(open_deps: list[int]) -> str:
    """Render the advisory, or '' when there's nothing to say."""
    if not open_deps:
        return ""
    refs = ", ".join(f"#{n}" for n in open_deps)
    return (
        f"  ⚠ This task depends on still-open task(s): {refs}.\n"
        "    Their results may change what you need to prove. Proceeding "
        "is allowed\n    but consider claiming a task with settled "
        "dependencies instead."
    )
