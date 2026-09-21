"""Claim-to-workable: build the workspace for a task you already hold.

`client.lease.claim` takes the lease and nothing else. This is the step
after it: re-fetch the issue for its canonical record, assemble the
workspace, post the first heartbeat, and collect the soft advisories the
claim did not block on.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from client import github as gh
from client.build_store import (
    format_cache_report,
    prepare_workspace_build_cache,
)
from client.deps import find_open_deps, format_deps_report
from client.heartbeat import heartbeat
from client.pins import check_pins, format_pin_report
from client.tooling import mathlib_search_advisory
from client.workspace import (
    WorkspaceError,
    branch_name,
    setup_workspace,
    workspace_profile,
)
from gate.state.intake import ParseSuccess, parse_issue_body
from gate.state.task_record import TaskRecord


@dataclass(frozen=True)
class PreparedTask:
    """A claimed task, ready to work.

    `advisories` are soft findings the claim did not block on: a pinned
    tool version that does not match, a still-open dependency, a missing
    Mathlib search tool. Read them; none of them stops you working.
    """

    path: Path
    record: TaskRecord
    branch: str
    advisories: tuple[str, ...] = ()


def prepare_task(repo: str, issue: int, *, session: str = "") -> PreparedTask:
    """Build the workspace for a task whose lease you already hold.

    Pass the `session` from the `ClaimResult` that won: it is written into
    the workspace so later beats and re-claims present the same identity.

    Call this after `client.lease.claim` returns `WON`. It re-fetches the
    issue for its canonical record, clones the project at the pinned
    commit onto a topic branch, drops `TASK.md` / `CHOIR.md` /
    `.choir-context.md`, and posts the first heartbeat.

    Raises `WorkspaceError` if the issue no longer parses or the clone
    fails — the lease is still yours at that point, so release it or fix
    the cause and retry.
    """

    issue_view = gh.get_issue(repo, issue)
    parsed = parse_issue_body(issue_view.body, expected_repo=repo)
    if not isinstance(parsed, ParseSuccess):
        raise WorkspaceError(
            f"issue #{issue} no longer parses as a task; the lease is held "
            "but no workspace was set up"
        )

    self_login = gh.current_user()
    path = setup_workspace(
        repo=repo,
        issue=issue,
        record=parsed.record,
        body_prose=parsed.body_prose,
        claimed_by=self_login,
        session=session,
    )

    # First beat of the lease; every later beat edits this comment in place.
    # `login` is passed because it is already resolved.
    heartbeat(repo, issue, login=self_login, session=session)

    # Fill `.lake` from the machine-local stores before any build runs.
    # One prebuilt copy per dependency set is cloned into each workspace
    # (design note 09), so N tasks on one machine cost one download
    # rather than N — several gigabytes each. Best-effort: on any failure
    # the workspace simply fetches its own, which is what it did before.
    cache_outcome = prepare_workspace_build_cache(path)

    advisories = []
    if cache_note := format_cache_report(cache_outcome):
        advisories.append(cache_note)
    if pin_report := format_pin_report(check_pins(path)):
        advisories.append(pin_report)
    profile = workspace_profile(path)
    if profile.search_tooling_note and (
        search := mathlib_search_advisory(path)
    ):
        advisories.append(search)
    if parsed.record.deps and (
        deps := format_deps_report(find_open_deps(repo, parsed.record.deps))
    ):
        advisories.append(deps)

    return PreparedTask(
        path=path,
        record=parsed.record,
        branch=branch_name(issue, parsed.record.target_decl or ""),
        advisories=tuple(advisories),
    )
