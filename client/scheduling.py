"""Claim ordering: the sequence a project wants its tasks worked in.

Ordering: priority descending, then age ascending — issue number is
the age proxy (GitHub numbers are monotonic). Difficulty is a FILTER,
never an ordering key: a high-priority hard task must not lose to a
low-priority easy one for a worker able to take both.

Every filter fails open on absent labels: no type label / no
difficulty label / unknown cap values all pass. Absence of a label
must never make a task unclaimable.
"""

from __future__ import annotations

from client.github import Issue
from gate.state.labels import Difficulty, parse_priority

TYPE_LABEL_PREFIX = "choir/type:"

_DIFFICULTY_CAPS = {
    "easy": Difficulty.EASY,
    "medium": Difficulty.MEDIUM,
    "hard": Difficulty.HARD,
}


def order_candidates(issues: list[Issue]) -> list[Issue]:
    """Priority desc, then issue number asc (age proxy)."""
    return sorted(issues, key=lambda i: (-int(parse_priority(i.labels)), i.number))
