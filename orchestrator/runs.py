"""Workflow runs held for approval — the gate's blind spot under open contribution.

Since spec D4 every contribution arrives as a PR from a contributor's fork,
and GitHub does not run fork-PR workflows unconditionally. On a **public**
repo the policy is one of three values (`gate/../new-project.sh` sets the most
permissive at bootstrap), and there is deliberately **no "never require
approval"** option — so some PRs will always land with their runs held. On a
**private** repo, fork-PR workflows are off entirely until the overseer turns
them on.

What that looks like from the orchestrator's side is a PR with **no checks at
all**. `merge_pr` refuses it — `missing_required_checks` reads every required
name as absent, which is the correct fail-safe and exactly what it should do
for a PR that deleted a workflow. But the two situations need opposite
responses: one is a PR to reject, the other is a contribution waiting on a
click. Without this module the orchestrator cannot tell them apart, and the
first contribution from every new contributor silently stalls.

**Approving is a judgment call, not a formality.** It runs a stranger's code
on your Actions runners. The blast radius is bounded — a fork-PR run gets a
read-only `GITHUB_TOKEN` and no secrets, and Choir's one `pull_request_target`
workflow never checks out PR code — so what is at risk is compute, not
credentials. The orchestrator should therefore read the diff first and apply
the same protected-path rule it already applies before merging
(`docs/agents/ORCHESTRATOR.md` § Boundaries): a PR touching `.github/`, `.choir/`,
`pyproject.toml` or a toolchain/manifest file is refused outright rather than
approved.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

# Runs held for approval report this status. Distinct from `waiting`, which
# means a deployment-environment gate — a different mechanism with a
# different approval endpoint, and not something Choir projects use.
PENDING_STATUS = "action_required"


class RunError(RuntimeError):
    """A `gh` operation failed in a workflow-run primitive."""


@dataclass(frozen=True)
class PendingRun:
    """One workflow run held awaiting approval."""

    run_id: int
    name: str          # the workflow's name, e.g. "verify-axiom-honesty"
    head_sha: str
    head_branch: str
    event: str
    actor: str         # who pushed — under D4, the contributor
    url: str


def _gh(*args: str) -> str:
    try:
        result = subprocess.run(
            ["gh", *args], capture_output=True, text=True, check=True
        )
        return result.stdout
    except FileNotFoundError as e:
        raise RunError(
            "`gh` CLI not found in PATH — install it from https://cli.github.com"
        ) from e
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or "").strip()
        raise RunError(f"gh {' '.join(args)} failed — {stderr}") from e


def pending_approvals(repo: str, *, limit: int = 100) -> list[PendingRun]:
    """Workflow runs in `repo` that are held awaiting a maintainer's approval."""
    out = _gh(
        "api",
        f"repos/{repo}/actions/runs?status={PENDING_STATUS}&per_page={limit}",
    )
    try:
        payload = json.loads(out)
    except json.JSONDecodeError as e:
        raise RunError(f"unreadable response listing held runs for {repo}") from e
    runs = payload.get("workflow_runs") or []
    return [
        PendingRun(
            run_id=int(r["id"]),
            name=str(r.get("name") or ""),
            head_sha=str(r.get("head_sha") or ""),
            head_branch=str(r.get("head_branch") or ""),
            event=str(r.get("event") or ""),
            actor=str((r.get("actor") or {}).get("login") or ""),
            url=str(r.get("html_url") or ""),
        )
        for r in runs
    ]


def approve_run(repo: str, run_id: int) -> None:
    """Release one held run so its workflow actually executes."""
    _gh("api", "-X", "POST", f"repos/{repo}/actions/runs/{run_id}/approve")


def approve_runs_for_sha(repo: str, head_sha: str) -> list[PendingRun]:
    """Approve every held run for `head_sha`; return the ones approved.

    Keyed on the head SHA rather than the PR number **because the API leaves
    us no choice**: a workflow run triggered by a fork PR reports an empty
    `pull_requests` array, so a run cannot be mapped back to its PR from the
    run object. The head SHA is the join key that does survive, and it is
    the right one anyway — it names the exact tree being approved, so a
    contributor pushing again after the orchestrator read the diff produces
    a new SHA whose runs are *not* covered by an earlier approval.

    Approval is per-run and does not persist: until a contributor has a
    merged commit they remain a first-time contributor, so every push to the
    PR raises a fresh set of held runs. The orchestrator re-checks each loop
    rather than approving once and assuming.
    """
    approved: list[PendingRun] = []
    for run in pending_approvals(repo):
        if run.head_sha == head_sha:
            approve_run(repo, run.run_id)
            approved.append(run)
    return approved
