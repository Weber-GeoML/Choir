"""Priority / difficulty label vocabulary (design note 11 §4).

Two orchestrator-owned label namespaces, disjoint from the lifecycle
labels (`choir/available` etc.), so the single-state-label invariant
is untouched. Absence semantics are load-bearing:

- no priority label   -> Priority.NORMAL (there is NO :normal label)
- no difficulty label -> None (unrated; passes every appetite filter)

The orchestrator is the only *writer* of these labels; client code
only parses and sorts. Unknown label values parse as absent — a
mangled label must never make a task unclaimable.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import IntEnum

PRIORITY_PREFIX = "choir/priority:"
DIFFICULTY_PREFIX = "choir/difficulty:"


class Priority(IntEnum):
    LOW = 0
    NORMAL = 1
    HIGH = 2


class Difficulty(IntEnum):
    EASY = 0
    MEDIUM = 1
    HARD = 2


_PRIORITY_BY_NAME = {"low": Priority.LOW, "high": Priority.HIGH}
_DIFFICULTY_BY_NAME = {
    "easy": Difficulty.EASY,
    "medium": Difficulty.MEDIUM,
    "hard": Difficulty.HARD,
}


def priority_label(p: Priority) -> str | None:
    """The label expressing `p`, or None for NORMAL (absence = normal)."""
    if p == Priority.NORMAL:
        return None
    return f"{PRIORITY_PREFIX}{p.name.lower()}"


def difficulty_label(d: Difficulty) -> str:
    return f"{DIFFICULTY_PREFIX}{d.name.lower()}"


def parse_priority(labels: Iterable[str]) -> Priority:
    """Priority from an issue's label set. Absent/unknown -> NORMAL."""
    for lbl in labels:
        if lbl.startswith(PRIORITY_PREFIX):
            p = _PRIORITY_BY_NAME.get(lbl[len(PRIORITY_PREFIX):])
            if p is not None:
                return p
    return Priority.NORMAL


def parse_difficulty(labels: Iterable[str]) -> Difficulty | None:
    """Difficulty from a label set. None = unrated."""
    for lbl in labels:
        if lbl.startswith(DIFFICULTY_PREFIX):
            d = _DIFFICULTY_BY_NAME.get(lbl[len(DIFFICULTY_PREFIX):])
            if d is not None:
                return d
    return None


def escalate(p: Priority) -> Priority:
    """One tier up, capped at HIGH — the orchestrator's aging bump."""
    return Priority(min(int(p) + 1, int(Priority.HIGH)))
