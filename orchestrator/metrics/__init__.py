"""Read-only metrics primitives for Choir project repos.

Walks the Choir-labeled issues in a repo and returns one structured
record per task (lifecycle timestamps, contributor at close, labels,
optionally the parsed TaskRecord). The CLI dumps these as JSON or CSV.

This is a *primitive*, not a dashboard. Choir does not ship
leaderboards, plots, or per-benchmark rollups — the overseer composes
those on top of the records this module produces.

Privacy: only public GitHub data is read (issues + their labels +
assignees). No contributor cost / API-spend data crosses the
orchestrator surface (load-bearing per AGENTS.md non-negotiable).
"""

from orchestrator.metrics.collect import (
    MetricsError,
    TaskMetric,
    collect_task_metrics,
)

__all__ = ["MetricsError", "TaskMetric", "collect_task_metrics"]
