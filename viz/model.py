"""Data model for the proof-tree timeline.

Three record kinds. `Event` is one dated thing that happened, `Node` is a
declaration the plan knows about, and `Edge` is a declared dependency
between two of them. A renderer replays events in order against the node
and edge sets to get the tree's state at any point.

Everything here is derived from a project's own history — its git log,
its issues, its pull requests. Nothing in this package writes to a
project, and no field requires a prover to run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum


def utc(stamp: str) -> str:
    """Every timestamp in one timezone, so the string sort is the time sort.

    Git reports the committer's local offset and GitHub reports UTC, and
    the timeline orders events by comparing the strings. Mixed offsets
    therefore misplaced every git-derived event by the offset — on a
    -04:00 machine a merge sorted four hours early, ahead of the very
    publication it answered.
    """
    if not stamp:
        return ""
    try:
        moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return stamp
    if moment.tzinfo is None:
        return stamp
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Actor(StrEnum):
    """Who did a thing.

    Role follows the action, not the login. The orchestrator is the only
    party with write access to the protected branch, so a direct push is
    the orchestrator by construction, while a pull request from a
    `choir/<n>-…` branch is a worker. In a single-contributor project
    both are the same GitHub login, which is why the login alone cannot
    tell them apart.
    """

    ORCHESTRATOR = "orchestrator"
    WORKER = "worker"


class EventKind(StrEnum):
    """What happened. Ordered roughly by a node's lifecycle."""

    NODE_STATED = "node_stated"
    TASK_PUBLISHED = "task_published"
    TASK_CLAIMED = "task_claimed"
    TASK_RELEASED = "task_released"
    PR_OPENED = "pr_opened"
    PR_MERGED = "pr_merged"
    PR_CLOSED = "pr_closed"
    REDUCTION_ACCEPTED = "reduction_accepted"
    NODE_FILLED = "node_filled"
    ORCHESTRATOR_COMMIT = "orchestrator_commit"
    RETRACTED = "retracted"
    NODE_REMOVED = "node_removed"


LIFECYCLE = (
    EventKind.NODE_STATED,
    EventKind.TASK_PUBLISHED,
    EventKind.TASK_CLAIMED,
    EventKind.TASK_RELEASED,
    EventKind.PR_OPENED,
    EventKind.REDUCTION_ACCEPTED,
    EventKind.NODE_FILLED,
    EventKind.PR_MERGED,
    EventKind.PR_CLOSED,
    EventKind.ORCHESTRATOR_COMMIT,
    EventKind.RETRACTED,
    EventKind.NODE_REMOVED,
)
"""Order to read two events that happened at the same instant.

A merge and the proof it delivered are one act recorded twice, and they
carry the same timestamp once the proof is dated at the merge. Read
merge-first, the node spends a step unclaimed and unfilled — a state it
was never in. So a proof reads before the merge that carried it.
"""

KIND_RANK = {kind: rank for rank, kind in enumerate(LIFECYCLE)}
assert set(KIND_RANK) == set(EventKind), "every kind needs a place in the order"


@dataclass(frozen=True)
class Identity:
    """A worker's identity: a login, and a session under it.

    One login can run several sessions at once, so the session is the
    fine grain. Claims made before the session field existed carry a
    login and no session; those are not one worker, they are workers we
    cannot tell apart, and a renderer must not draw them as one.
    """

    login: str
    session: str = ""

    @property
    def distinguishable(self) -> bool:
        return bool(self.session)

    @property
    def label(self) -> str:
        if self.session:
            return f"{self.login}/{self.session[:8]}"
        return f"{self.login}/unattributed"


@dataclass(frozen=True)
class Event:
    """One dated thing that happened, attributed to an actor."""

    at: str  # ISO 8601, UTC
    kind: EventKind
    actor: Actor
    decl: str = ""  # the declaration this concerns, when it concerns one
    issue: int = 0
    pr: int = 0
    sha: str = ""
    identity: Identity | None = None
    detail: str = ""
    seq: int = 0
    """Commit sequence, for events a commit produced; 0 otherwise.

    Timestamps are second-granularity, so a bulk push puts many commits
    in one second. Ordering by sequence within a timestamp keeps them in
    the order they were actually made.
    """

    @property
    def sort_key(self) -> tuple[str, int, str]:
        return (self.at, self.seq, self.kind.value)


@dataclass
class Node:
    """A declaration the plan knows about."""

    decl: str
    kind: str = "theorem"
    """`theorem` or `definition`, when the plan says. Drives the shape."""

    file: str = ""
    group: str = ""  # the roadmap group this sits in, when the plan says
    doc: str = ""  # prose reference, when the plan gives one
    stated_at: str = ""
    body_filled_at: str = ""
    issue: int = 0
    declares_deps: bool = False
    """Whether the plan declares *any* dependency for this node.

    A node with no declared dependency is indistinguishable from a real leaf
    by structure alone, so a renderer marks it rather than drawing a leaf
    the plan never claimed.
    """

    given: bool = False
    """Complete, with no commit in this project that shows it.

    Two ways that happens: the prover's library provides the result
    (`upstream`), or the prover named the declaration itself because the
    source wrote none, leaving the text scan nothing to key a history
    to. Either way a commit-derived timeline can never fill it, and
    everything resting on it would read as unfinished forever."""

    upstream: bool = False
    """Declared by a library rather than by this project.

    The narrower half of `given`, and the one with no history of its own
    to read: the project never wrote this declaration, so no commit here
    can date it. It is in the tree because something here uses it, which
    is also when it belongs on screen.
    """


@dataclass(frozen=True)
class Edge:
    """A declared dependency: `child` is needed by `parent`."""

    parent: str
    child: str
    source: str  # "reduction" | "graph.json"
    at: str = ""
    pr: int = 0


@dataclass
class Timeline:
    """The whole derivation: what exists, how it connects, what happened."""

    repo: str
    prover: str
    nodes: dict[str, Node] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    identities: dict[str, Identity] = field(default_factory=dict)
    ambiguous: dict[str, list[str]] = field(default_factory=dict)
    """Declaration names the plan gives to more than one node.

    Kept apart rather than merged: drawing one node where the project
    has two would be a quieter error than reporting the collision.
    """

    notes: list[str] = field(default_factory=list)
    """Things the derivation could not establish, stated rather than hidden."""
