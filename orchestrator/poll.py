"""Deterministic change-poller — the orchestrator loop's clock.

An LLM agent has no innate timer; "check every 10 minutes" is prose
until something mechanical does the waiting. This module is that
mechanism: it snapshots the repo's Choir-relevant state (open PRs +
their check status, open task issues + their labels), then re-checks
on a fixed interval and **exits as soon as something changed**,
printing a JSON summary of what. The agent runs it as a blocking /
background subprocess and reacts when it exits — zero LLM cost while
idle, and the cadence is enforced by the subprocess, not by the
agent's memory.

CLI::

    python -m orchestrator.poll --repo owner/name \
        [--interval 600] [--max-wait 3600] [--once] [--prover lean4]

`--repo` / `--interval` / `--max-wait` default to the values in
`~/.choir/orchestrator.toml` (see `orchestrator.local_config`).
`--once` prints the current snapshot and exits (initial stock-take).
`--prover` (default: unset, strict) threads the project's prover into the
mergeability read — this poller has no local checkout to resolve it from
`.choir/project.toml` itself, unlike `merge_pr`'s caller, so pass it
explicitly (see `main`'s `--prover` help for the concrete divergence this
closes).

Exit codes:
    0 — change detected (JSON summary on stdout)
    1 — bad arguments / no repo configured
    2 — GitHub error on the initial snapshot (auth, network, repo)
    3 — max-wait reached with no change (JSON with empty changes)

Transient errors *during* the wait are tolerated (logged to stderr,
retried next tick) so an overnight run survives rate-limit blips.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass

from gate.provers import ProverError, get_profile
from orchestrator.local_config import read_local_config
from orchestrator.prs import (
    PRError,
    PRView,
    blocking_failures,
    list_open_prs,
    missing_required_checks,
    pending_blocking_checks,
)
from orchestrator.tasks import MaintainerError, TaskHandle, list_choir_tasks


def _checks_state(pr: PRView, *, prover: str | None = None) -> str:
    """"pending" / "green" / "red" — mergeability, not every check.

    Deliberately the same predicates `merge_pr`'s preflight uses
    (`gate.checks` via `blocking_failures` / `missing_required_checks`), not
    `checks_all_green`, which counts advisory checks too. Two consequences of
    that older reading were wrong here: a PR failing only `style` or `review`
    read as red, and a slow-but-genuinely-advisory check still running after
    the blocking set went green would hold a fully-green PR at "pending"
    until it finished too. (`verify-comparator`'s own `timeout-minutes: 60`
    no longer illustrates the second case: the check is blocking now, note
    14 §8, so holding a PR pending while it runs is the correct read, not a
    bug — see `test_pending_comparator_holds_a_pr_pending` in
    `tests/orchestrator/test_poll.py`.)

    `prover` is forwarded to `blocking_failures` / `pending_blocking_checks`
    unchanged — never defaulted to a specific prover here — so this reads
    mergeability *the way `merge_pr` does* only when both are called with the
    same prover; the default `None` is the strict reading both fall back to.
    `missing_required_checks` takes no `prover`: presence is not
    prover-dependent (a per-prover-inapplicable audit like `comparator`
    reports a green not-applicable line rather than being
    absent — see `gate.checks.REQUIRED_PRESENT`'s docstring), so there is
    nothing for a keyword to mean there.

    Trade-off, accepted: an advisory check finishing *after* the blocking set
    goes green produces no state transition, so no wakeup. The orchestrator
    reads the full PR when it acts on the green, and can see there that an
    advisory check is still running.
    """
    if pending_blocking_checks(pr, prover=prover):
        return "pending"
    if blocking_failures(pr, prover=prover) or missing_required_checks(pr):
        return "red"
    return "green"


@dataclass(frozen=True)
class Snapshot:
    """Comparable fingerprint of the repo's Choir-relevant state."""

    # (number, head_sha, checks_state)
    prs: tuple[tuple[int, str, str], ...]
    # (number, "label1,label2,...", updated_at) — sorted for stability.
    #
    # `updated_at` is what makes a claim visible here. Under spec D4 a
    # claim is a comment and a contributor cannot write labels, so a
    # snapshot of labels alone does not change when a task is taken: the
    # poller sleeps, the orchestrator never takes stock, and the board
    # keeps reading `choir/available` until the max-wait timeout. A
    # comment moves `updatedAt`, and every comment on a task issue —
    # claim, release, defect report — is something to react to.
    tasks: tuple[tuple[int, str, str], ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "open_prs": [
                {"number": n, "head_sha": s, "checks": c} for n, s, c in self.prs
            ],
            "open_tasks": [
                {
                    "number": n,
                    "labels": lbls.split(",") if lbls else [],
                    "updated_at": at,
                }
                for n, lbls, at in self.tasks
            ],
        }


def make_snapshot(
    prs: list[PRView], tasks: list[TaskHandle], *, prover: str | None = None
) -> Snapshot:
    """`prover` is forwarded to `_checks_state` unchanged (never defaulted to
    a specific prover here) — an omitted prover reads every PR strict, same
    as `_checks_state` itself.
    """
    return Snapshot(
        prs=tuple(
            sorted(
                (pr.number, pr.head_sha, _checks_state(pr, prover=prover))
                for pr in prs
            )
        ),
        tasks=tuple(
            sorted(
                (t.number, ",".join(sorted(t.labels)), t.updated_at)
                for t in tasks
            )
        ),
    )


def fetch_snapshot(repo: str, *, prover: str | None = None) -> Snapshot:
    return make_snapshot(
        list_open_prs(repo), list_choir_tasks(repo, state="open"), prover=prover
    )


def diff_snapshots(old: Snapshot, new: Snapshot) -> list[str]:
    """Human/agent-readable list of what changed. Empty = no change."""
    changes: list[str] = []

    old_prs = {n: (sha, st) for n, sha, st in old.prs}
    new_prs = {n: (sha, st) for n, sha, st in new.prs}
    for n in sorted(new_prs.keys() - old_prs.keys()):
        changes.append(f"PR #{n} opened (checks {new_prs[n][1]})")
    for n in sorted(old_prs.keys() - new_prs.keys()):
        changes.append(f"PR #{n} closed or merged")
    for n in sorted(old_prs.keys() & new_prs.keys()):
        old_sha, old_state = old_prs[n]
        new_sha, new_state = new_prs[n]
        if old_sha != new_sha:
            changes.append(f"PR #{n} got new commits")
        if old_state != new_state:
            changes.append(f"PR #{n} checks: {old_state} → {new_state}")

    old_tasks = {n: (lbls, at) for n, lbls, at in old.tasks}
    new_tasks = {n: (lbls, at) for n, lbls, at in new.tasks}
    for n in sorted(new_tasks.keys() - old_tasks.keys()):
        changes.append(f"task #{n} opened")
    for n in sorted(old_tasks.keys() - new_tasks.keys()):
        changes.append(f"task #{n} closed")
    for n in sorted(old_tasks.keys() & new_tasks.keys()):
        old_labels, old_at = old_tasks[n]
        new_labels, new_at = new_tasks[n]
        if old_labels != new_labels:
            changes.append(f"task #{n} labels: [{old_labels}] → [{new_labels}]")
        elif old_at != new_at:
            # A comment and nothing else. Under spec D4 that is how a
            # claim and a release arrive, so the labels are the stale
            # thing here, not the signal — reconcile them.
            changes.append(
                f"task #{n} commented on — read its lease and sync the labels"
            )

    return changes


def _emit(changes: list[str], waited: int, snapshot: Snapshot) -> None:
    json.dump(
        {
            "changes": changes,
            "waited_seconds": waited,
            "snapshot": snapshot.as_dict(),
        },
        sys.stdout,
        indent=2,
    )
    sys.stdout.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="orchestrator.poll",
        description="Block until the repo's Choir state changes, then exit.",
    )
    parser.add_argument(
        "--repo",
        default=None,
        help="owner/name (default: project.repo from "
        "~/.choir/projects/<owner>/<repo>/orchestrator.toml, "
        "or the legacy ~/.choir/orchestrator.toml)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=None,
        help="seconds between checks (default from local config, else 600)",
    )
    parser.add_argument(
        "--max-wait",
        type=int,
        default=None,
        help="give up after this many seconds (default from local config, else 3600)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="print the current snapshot and exit",
    )
    parser.add_argument(
        "--prover",
        default=None,
        help="the project's prover ('lean4' / 'isabelle' / 'rocq'), threaded "
        "into the mergeability read. This poller has no local checkout to "
        "read `.choir/project.toml` from (unlike `merge_pr`'s caller), so it "
        "cannot resolve this itself — pass it explicitly. Omit it and the "
        "poller answers strict: a lean4 PR whose only red check is "
        "`statement-equiv` (advisory on lean4, superseded by `comparator`) "
        "reads 'red' here even though `merge_pr(..., prover=\"lean4\")` "
        "would merge it.",
    )
    args = parser.parse_args(argv)

    if args.prover is not None:
        try:
            get_profile(args.prover)
        except ProverError as e:
            # Reuses gate.provers' own "unknown prover; valid: ..." message —
            # a typo'd --prover must be a loud error, not a silent fall-back
            # to the strict default (that would look identical to "I chose
            # strict on purpose").
            print(f"error: {e}", file=sys.stderr)
            return 1

    # The local config is keyed by repo, so resolve --repo first, then fill
    # interval / max-wait / repo defaults from whichever config that selects.
    cfg = read_local_config(repo=args.repo)
    if not args.repo:
        args.repo = cfg.repo
    if args.interval is None:
        args.interval = cfg.poll_interval_seconds
    if args.max_wait is None:
        args.max_wait = cfg.max_wait_seconds

    if not args.repo:
        print(
            "error: no repo — pass --repo or set project.repo in "
            "~/.choir/projects/<owner>/<repo>/orchestrator.toml",
            file=sys.stderr,
        )
        return 1

    try:
        baseline = fetch_snapshot(args.repo, prover=args.prover)
    except (PRError, MaintainerError) as e:
        print(f"error: initial snapshot failed — {e}", file=sys.stderr)
        return 2

    if args.once:
        _emit([], 0, baseline)
        return 0

    waited = 0
    while waited < args.max_wait:
        time.sleep(args.interval)
        waited += args.interval
        try:
            current = fetch_snapshot(args.repo, prover=args.prover)
        except (PRError, MaintainerError) as e:
            # Transient (rate limit, network blip): log and try next tick.
            print(f"warning: snapshot failed, retrying — {e}", file=sys.stderr)
            continue
        changes = diff_snapshots(baseline, current)
        if changes:
            _emit(changes, waited, current)
            return 0
        print(f"no change after {waited}s", file=sys.stderr)

    _emit([], waited, baseline)
    return 3


if __name__ == "__main__":
    sys.exit(main())
