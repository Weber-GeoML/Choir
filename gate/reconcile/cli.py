"""Reconciliation CLI — invoked by `.github/workflows/reconcile.yml`.

Lists currently `choir/claimed` issues, identifies stale ones via the
pure logic in `stale_claims.py`, and applies the cleanup via `gh`:
post a comment, remove `choir/claimed` and any heartbeat label, add
`choir/available`, and unassign all assignees.

The workflow runs daily. `--dry-run` lets a maintainer preview what
would happen without changing state.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from gate.reconcile.config import (
    DEFAULT_STALE_AFTER_DAYS,
    ReconcileConfigError,
    read_reconcile_config,
)
from gate.reconcile.stale_claims import (
    LABEL_CLAIMED,
    IssueView,
    StaleClaim,
    identify_stale,
    utc_now,
)

LABEL_AVAILABLE = "choir/available"


def _gh(*args: str) -> str:
    """Run `gh` with the given args. Stderr is forwarded on failure."""
    result = subprocess.run(
        ["gh", *args], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"gh {' '.join(args)} failed: {(result.stderr or '').strip()}"
        )
    return result.stdout


def fetch_claimed(repo: str) -> list[IssueView]:
    """Fetch all open `choir/claimed` issues in `repo` as `IssueView`s."""
    raw = _gh(
        "issue",
        "list",
        "--repo",
        repo,
        "--label",
        LABEL_CLAIMED,
        "--state",
        "open",
        "--json",
        "number,labels,updatedAt,assignees",
        "--limit",
        "1000",
    )
    items: list[dict[str, Any]] = json.loads(raw)
    return [
        IssueView(
            number=item["number"],
            labels=[lbl["name"] for lbl in item.get("labels", [])],
            updated_at=item.get("updatedAt", ""),
            assignees=[u["login"] for u in item.get("assignees", [])],
        )
        for item in items
    ]


def apply_stale(repo: str, claim: StaleClaim, issue: IssueView) -> None:
    """Apply the reclamation actions for one stale claim."""
    _gh(
        "issue",
        "comment",
        str(claim.number),
        "--repo",
        repo,
        "--body",
        (
            "Reconciliation: this claim is being reclaimed — "
            f"{claim.reason}. The issue is back to `choir/available`."
        ),
    )
    if claim.heartbeat_label:
        _gh(
            "issue",
            "edit",
            str(claim.number),
            "--repo",
            repo,
            "--remove-label",
            claim.heartbeat_label,
        )
    _gh(
        "issue",
        "edit",
        str(claim.number),
        "--repo",
        repo,
        "--remove-label",
        LABEL_CLAIMED,
        "--add-label",
        LABEL_AVAILABLE,
    )
    for assignee in issue.assignees:
        # remove-assignee is best-effort; if a manual cleanup already
        # ran, the API returns no-op.
        with contextlib.suppress(RuntimeError):
            _gh(
                "issue",
                "edit",
                str(claim.number),
                "--repo",
                repo,
                "--remove-assignee",
                assignee,
            )


def _resolve_threshold(flag_value: int | None, config_root: Path) -> int:
    """Pick the staleness threshold.

    Precedence: an explicit ``--threshold-days`` flag wins (an operator
    running a manual one-off), else `[reconcile].stale_after_days` from the
    project config, else the built-in default. A malformed config section
    warns and falls back to the default rather than crashing the daily job.
    """
    if flag_value is not None:
        return flag_value
    try:
        return read_reconcile_config(config_root).stale_after_days
    except ReconcileConfigError as e:
        print(
            f"warning: {e}; using default {DEFAULT_STALE_AFTER_DAYS} days",
            file=sys.stderr,
        )
        return DEFAULT_STALE_AFTER_DAYS


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Choir stale-claim reconciliation")
    parser.add_argument(
        "--repo", required=True, help="GitHub repo as owner/name"
    )
    parser.add_argument(
        "--threshold-days",
        type=int,
        default=None,
        help=(
            "Override the staleness threshold in days. Default: read "
            "[reconcile].stale_after_days from .choir/project.toml, "
            f"else {DEFAULT_STALE_AFTER_DAYS}."
        ),
    )
    parser.add_argument(
        "--config-root",
        type=Path,
        default=Path("."),
        help=(
            "Checkout root holding .choir/project.toml, consulted when "
            "--threshold-days is not given (default: cwd)"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List stale claims but make no changes",
    )
    args = parser.parse_args(argv)

    threshold_days = _resolve_threshold(args.threshold_days, args.config_root)

    try:
        issues = fetch_claimed(args.repo)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if not issues:
        print(f"No claimed issues in {args.repo}.")
        return 0

    stale = identify_stale(issues, now=utc_now(), threshold_days=threshold_days)

    print(
        f"Scanned {len(issues)} claimed issue(s) in {args.repo} "
        f"(threshold {threshold_days}d); {len(stale)} stale."
    )
    if not stale:
        return 0

    issues_by_number = {i.number: i for i in issues}
    for sc in stale:
        print(f"  #{sc.number}: {sc.reason}")
        if args.dry_run:
            continue
        try:
            apply_stale(args.repo, sc, issues_by_number[sc.number])
            print(f"  #{sc.number}: reclaimed → choir/available")
        except RuntimeError as e:
            print(f"  #{sc.number}: failed to reclaim: {e}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
