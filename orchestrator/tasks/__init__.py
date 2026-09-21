"""Maintainer/overseer-side library API.

Contributors use `client/` to claim / work / submit. Maintainers and
project overseers use this module to do the inverse:

- create Choir-shaped issues programmatically (batch-import a benchmark,
  generate per-declaration tasks from a sorries scan, etc.);
- list and read back Choir issues to drive bespoke tooling;
- aggregate task state for dashboards / leaderboards.

The functions here are thin wrappers over `gh issue create / list /
view`; the value is the round-trip guarantee with the intake parser
(serialized issue bodies pass `gate.state.intake.parse_issue_body`).

This module is the *primitive layer*. Higher-level orchestration
(benchmark importers, leaderboards, automated retries) is the
overseer's responsibility — they compose these primitives in a script
or a custom orchestrator service. Choir doesn't ship benchmark
specifics.
"""

from orchestrator.tasks.api import (
    MaintainerError,
    TaskHandle,
    TaskView,
    create_task_issue,
    get_task,
    list_choir_tasks,
)
from orchestrator.tasks.serialize import (
    body_to_task,
    task_to_body,
)

__all__ = [
    "MaintainerError",
    "TaskHandle",
    "TaskView",
    "body_to_task",
    "create_task_issue",
    "get_task",
    "list_choir_tasks",
    "task_to_body",
]
