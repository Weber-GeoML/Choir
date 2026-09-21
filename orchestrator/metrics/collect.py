"""Walk Choir-labeled issues in a repo and emit structured per-task metrics.

The collected record per task is intentionally narrow:

- `number`, `title`, `state`, `labels`, `url` — issue identity.
- `task_type` — parsed out of the body if present, None if the body
  doesn't validate (the issue is still reported).
- `created_at`, `closed_at` — ISO 8601 strings from GitHub.
- `duration_seconds` — `closed_at - created_at` when both are set,
  else None. This is *issue lifetime*, not "time to solve" (we don't
  have claim timestamps in the bulk issue payload). A finer-grained
  metric would do an events query per closed issue — left for the
  overseer to add if they need it.
- `contributor` — the assignee at close (single login). If multiple
  assignees, the first is reported (Choir's lease model uses one
  assignee per claim).

The collector is read-only; it never mutates any GitHub state.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime

from gate.state.task_record import TaskType
from orchestrator.tasks.serialize import body_to_task


class MetricsError(RuntimeError):
    """A `gh` call failed during metrics collection."""


@dataclass(frozen=True)
class TaskMetric:
    """One Choir issue's lifecycle metrics."""

    number: int
    title: str
    url: str
    state: str
    labels: tuple[str, ...]
    task_type: str | None
    created_at: str
    closed_at: str | None
    duration_seconds: int | None
    contributor: str | None

    def as_dict(self) -> dict[str, object]:
        # tuple → list for clean JSON encoding.
        d = asdict(self)
        d["labels"] = list(self.labels)
        return d


def _gh(*args: str) -> str:
    try:
        result = subprocess.run(
            ["gh", *args], capture_output=True, text=True, check=True
        )
        return result.stdout
    except FileNotFoundError as e:
        raise MetricsError(
            "`gh` CLI not found in PATH — install it from https://cli.github.com"
        ) from e
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or "").strip()
        raise MetricsError(f"gh {' '.join(args)} failed — {stderr}") from e


def _duration_seconds(created: str, closed: str | None) -> int | None:
    if not closed:
        return None
    try:
        c0 = datetime.fromisoformat(created.replace("Z", "+00:00"))
        c1 = datetime.fromisoformat(closed.replace("Z", "+00:00"))
    except ValueError:
        return None
    return int((c1 - c0).total_seconds())


def collect_task_metrics(
    repo: str,
    *,
    state: str = "all",
    label: str | None = None,
    limit: int = 1000,
) -> list[TaskMetric]:
    """Return one TaskMetric per Choir-labeled issue in `repo`.

    Filters mirror :func:`orchestrator.tasks.list_choir_tasks`:

    - ``state``: ``"open"``, ``"closed"``, or ``"all"`` (default).
    - ``label``: restrict to an additional label (e.g.,
      ``"benchmark/quals2026"``).
    - ``limit``: caps the number of issues returned. Defaults to 1000
      which matches `gh issue list`'s max; raise this only if you
      know you have more — the GitHub paginate ceiling is its own
      concern beyond that.
    """
    cmd = [
        "issue", "list",
        "--repo", repo,
        "--label", "choir/task",
        "--state", state,
        "--json", "number,title,url,state,labels,body,createdAt,closedAt,assignees",
        "--limit", str(limit),
    ]
    if label is not None:
        cmd.extend(["--label", label])

    out = _gh(*cmd)
    items = json.loads(out)

    metrics: list[TaskMetric] = []
    for it in items:
        labels = tuple(lbl["name"] for lbl in it.get("labels", []))
        body = it.get("body") or ""
        task_type: str | None = None
        try:
            record, _ = body_to_task(body)
            task_type = record.type.value
        except ValueError:
            # Body doesn't validate; surface the issue without a type
            # so the maintainer can still see broken issues in metrics.
            for lbl in labels:
                if lbl.startswith("choir/type:"):
                    candidate = lbl.split(":", 1)[1]
                    if candidate in {t.value for t in TaskType}:
                        task_type = candidate
                        break

        assignees = it.get("assignees") or []
        contributor: str | None = None
        if assignees:
            contributor = assignees[0]["login"]

        created_at = it.get("createdAt", "")
        closed_at = it.get("closedAt") or None
        duration = _duration_seconds(created_at, closed_at)

        metrics.append(
            TaskMetric(
                number=it["number"],
                title=it["title"],
                url=it["url"],
                state=it["state"].lower(),
                labels=labels,
                task_type=task_type,
                created_at=created_at,
                closed_at=closed_at,
                duration_seconds=duration,
                contributor=contributor,
            )
        )
    return metrics
