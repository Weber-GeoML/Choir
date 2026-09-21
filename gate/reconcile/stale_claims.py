"""Pure logic for identifying stale Choir claims.

The reconciliation workflow runs this against the list of currently
claimed issues. An issue is stale if it has been `choir/claimed` long
enough without a fresh heartbeat that the lease should be reclaimed.

Two staleness signals:

1. A `choir/heartbeat:YYYY-MM-DD` label whose date is older than the
   threshold. This is the primary signal — `choir claim` and `choir
   work` both heartbeat, so an active contributor produces fresh
   labels naturally.
2. *No* heartbeat label, plus `updated_at` older than the threshold.
   This covers legacy claims from before heartbeat was added, and
   claims taken by tooling that doesn't heartbeat.

Issues without `choir/claimed` are not in scope here at all. (A
claimed issue with a future-dated heartbeat — which shouldn't happen
but is conceivable from clock skew — is treated as fresh.)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

LABEL_CLAIMED = "choir/claimed"
HEARTBEAT_PREFIX = "choir/heartbeat:"


@dataclass(frozen=True)
class IssueView:
    """Minimal projection used by the reconcile pass."""

    number: int
    labels: list[str]
    updated_at: str  # ISO 8601 with Z or +00:00
    assignees: list[str]


@dataclass(frozen=True)
class StaleClaim:
    """An issue whose lease should be reclaimed, with a human-readable reason."""

    number: int
    reason: str
    heartbeat_label: str | None  # the stale heartbeat label, if any


def identify_stale(
    issues: list[IssueView],
    *,
    now: datetime,
    threshold_days: int = 7,
) -> list[StaleClaim]:
    """Return the subset of `issues` whose claim is stale.

    `now` should be in UTC. `threshold_days` is the maximum age (in
    whole days) of either the heartbeat or `updated_at` before a claim
    is considered orphaned.
    """
    out: list[StaleClaim] = []
    threshold = timedelta(days=threshold_days)
    today = now.date()

    for issue in issues:
        if LABEL_CLAIMED not in issue.labels:
            continue

        heartbeat_lbl, heartbeat_date = _find_latest_heartbeat(issue.labels)
        if heartbeat_date is not None:
            age = today - heartbeat_date
            if age > threshold:
                out.append(
                    StaleClaim(
                        number=issue.number,
                        reason=(
                            f"heartbeat is {age.days} days old "
                            f"(>{threshold_days} day threshold)"
                        ),
                        heartbeat_label=heartbeat_lbl,
                    )
                )
            continue

        # No heartbeat label at all — fall back to updated_at.
        updated = _parse_iso(issue.updated_at)
        if updated is None:
            continue
        age_seconds = (now - updated).total_seconds()
        age_days = int(age_seconds // 86400)
        if age_seconds > threshold.total_seconds():
            out.append(
                StaleClaim(
                    number=issue.number,
                    reason=(
                        f"no heartbeat label; last activity {age_days} days ago "
                        f"(>{threshold_days} day threshold)"
                    ),
                    heartbeat_label=None,
                )
            )

    return out


def _find_latest_heartbeat(labels: list[str]) -> tuple[str | None, date | None]:
    """Among `labels`, return (label_name, parsed_date) of the latest heartbeat.

    Returns (None, None) if no heartbeat label is present. If multiple
    heartbeat labels exist (which shouldn't happen — `heartbeat_operations`
    self-heals duplicates — but tolerate it), pick the most recent date.
    """
    latest_label: str | None = None
    latest_date: date | None = None
    for lbl in labels:
        if not lbl.startswith(HEARTBEAT_PREFIX):
            continue
        try:
            d = date.fromisoformat(lbl[len(HEARTBEAT_PREFIX):])
        except ValueError:
            continue
        if latest_date is None or d > latest_date:
            latest_label = lbl
            latest_date = d
    return latest_label, latest_date


def _parse_iso(s: str) -> datetime | None:
    """Parse a GitHub-style ISO 8601 timestamp, returning a tz-aware datetime."""
    try:
        # GitHub returns `YYYY-MM-DDTHH:MM:SSZ`; fromisoformat in 3.11+
        # accepts that directly, but normalize Z to +00:00 for safety.
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def utc_now() -> datetime:
    """Wrapper for `datetime.now(timezone.utc)` to make tests easier."""
    return datetime.now(UTC)
