"""Turn a project's fetched history into a timeline. Pure.

Every input arrives as data — a `github.Payload`, the commit records from
`gitscan`, and optionally the plan's `graph.json` — so this module makes
no network calls, reads no files, and can be tested against recorded
payloads without mocks.

**Attribution.** Role follows the action. The orchestrator is the only
party that pushes to the protected branch, so a commit that is not some
merged pull request's merge commit is the orchestrator's. A commit that
*is* one belongs to that pull request's author — the worker who wrote it,
not the orchestrator who pressed merge. Reading that backwards would
credit the orchestrator with every merged proof.

**Identity.** A worker is a login and a session. The session lives only
in the lease comment, and only from protocol 8 onward, so claims made
earlier yield a login with no session. Those are not one worker; they are
workers that cannot be told apart, and `Identity.distinguishable` says
so rather than letting a renderer imply otherwise.
"""

from __future__ import annotations

import re
from typing import Any

from gate.state.intake import ParseSuccess, parse_issue_body
from gate.state.lease_comment import parse_lease_comment
from gate.state.reduction_record import ReductionRecord, parse_reduction_block
from orchestrator.metrics.struggle import issue_of_branch
from viz.github import Payload
from viz.gitscan import CommitRecord
from viz.model import Actor, Edge, Event, EventKind, Identity, Node, Timeline, utc

RETRACTED_LABEL = "choir/retracted"

# The session field as `client.lease` mints it. Read from the raw block
# rather than the parsed claim: a Choir checkout older than protocol 8
# has no `session` attribute to read, and this tool must work against
# whichever version a project is on.
_SESSION_RE = re.compile(r"^\s*session:\s*([0-9a-f]{8,})\s*$", re.MULTILINE)


def _session_of(body: str) -> str:
    match = _SESSION_RE.search(body)
    return match.group(1) if match else ""


def _leaf(decl: str) -> str:
    """A declaration's last dotted segment.

    The plan names declarations fully qualified (`PMC.sidorenko_tree`)
    while the scanner reports them as written on the declaration line
    (`sidorenko_tree` inside a `namespace PMC` block). Matching on the
    leaf is what joins the two views of one declaration.
    """
    return decl.rsplit(".", 1)[-1]


class _Names:
    """Resolves a scanned declaration onto the plan's name for it.

    A leaf shared by two plan nodes resolves to neither: merging them
    would draw one node where the project has two, so they stay apart
    and the ambiguity is reported.
    """

    def __init__(self, plan_decls: list[str]) -> None:
        seen: dict[str, list[str]] = {}
        for decl in plan_decls:
            seen.setdefault(_leaf(decl), []).append(decl)
        self._unique = {
            leaf: decls[0] for leaf, decls in seen.items() if len(decls) == 1
        }
        self.ambiguous = {
            leaf: decls for leaf, decls in seen.items() if len(decls) > 1
        }

    def canonical(self, scanned: str) -> str:
        return self._unique.get(_leaf(scanned), scanned)


def _labels(issue: dict[str, Any]) -> set[str]:
    return {label.get("name", "") for label in issue.get("labels") or []}


def _merge_shas(prs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Merge-commit SHA -> the pull request that produced it."""
    out: dict[str, dict[str, Any]] = {}
    for pr in prs:
        commit = pr.get("mergeCommit") or {}
        oid = commit.get("oid") or ""
        if oid and pr.get("mergedAt"):
            out[oid] = pr
    return out


def _task_decls(payload: Payload) -> dict[int, str]:
    """Issue number -> the declaration its task record targets."""
    out: dict[int, str] = {}
    for issue in payload.issues:
        parsed = parse_issue_body(issue.get("body") or "", expected_repo=payload.repo)
        if isinstance(parsed, ParseSuccess) and parsed.record.target_decl:
            out[issue["number"]] = parsed.record.target_decl
    return out


def _graph_nodes(graph: dict[str, Any] | None) -> tuple[dict[str, Node], list[Edge]]:
    """Nodes and declared edges from the plan, when the project keeps one.

    `uses` and `proof_uses` are the plan's own dependency lists, keyed by
    node id rather than declaration name, so ids are resolved to decls
    before an edge is emitted. A node the plan names but whose decl it
    omits cannot be placed and is skipped.
    """
    if not graph:
        return {}, []
    raw = graph.get("nodes") or {}
    decl_of = {nid: (n.get("decl") or "") for nid, n in raw.items()}
    nodes: dict[str, Node] = {}
    edges: list[Edge] = []
    for nid, spec in raw.items():
        decl = decl_of.get(nid) or ""
        if not decl or spec.get("kind") == "group":
            continue
        deps = list(spec.get("uses") or []) + list(spec.get("proof_uses") or [])
        nodes[decl] = Node(
            decl=decl,
            kind=spec.get("kind") or "theorem",
            file=spec.get("file") or "",
            group=spec.get("parent") or "",
            doc=spec.get("doc") or "",
            declares_deps=bool(deps),
            given=bool(spec.get("upstream")) or spec.get("proof") == "formalized",
            upstream=bool(spec.get("upstream")),
        )
        for dep in deps:
            child = decl_of.get(dep) or ""
            if child:
                edges.append(Edge(parent=decl, child=child, source="graph.json"))
    return nodes, edges


def derive(
    payload: Payload,
    commits: list[CommitRecord],
    *,
    prover: str = "lean4",
    graph: dict[str, Any] | None = None,
) -> Timeline:
    """Build the timeline. No I/O."""
    timeline = Timeline(repo=payload.repo, prover=prover)
    plan_nodes, plan_edges = _graph_nodes(graph)
    timeline.nodes.update(plan_nodes)
    timeline.edges.extend(plan_edges)

    by_merge = _merge_shas(payload.prs)
    names = _Names(list(plan_nodes))
    task_decl = {
        issue: names.canonical(decl)
        for issue, decl in _task_decls(payload).items()
    }
    pr_author: dict[int, str] = {
        pr["number"]: (pr.get("author") or {}).get("login", "") for pr in payload.prs
    }
    # Issue -> the identity that most recently claimed it, so a pull
    # request can inherit the session its claim recorded.
    claim_identity: dict[int, Identity] = {}

    _issue_events(payload, timeline, task_decl, claim_identity)
    index_of = {record.sha: record.index for record in commits}
    _pr_events(
        payload, timeline, task_decl, pr_author, claim_identity, index_of
    )
    _commit_events(
        commits, timeline, by_merge, pr_author, claim_identity, names
    )

    timeline.ambiguous = names.ambiguous
    timeline.events.sort(key=lambda e: e.sort_key)
    _settle_given(timeline)
    _drop_unused_upstream(timeline)
    _add_notes(timeline)
    return timeline


def _issue_events(
    payload: Payload,
    timeline: Timeline,
    task_decl: dict[int, str],
    claim_identity: dict[int, Identity],
) -> None:
    """Publications, claims, releases and retractions, from the issues."""
    for issue in payload.issues:
        number = issue["number"]
        labels = _labels(issue)
        decl = task_decl.get(number, "")
        if decl:
            timeline.events.append(
                Event(
                    at=utc(issue["createdAt"]),
                    kind=EventKind.TASK_PUBLISHED,
                    actor=Actor.ORCHESTRATOR,
                    decl=decl,
                    issue=number,
                )
            )
            node = timeline.nodes.setdefault(decl, Node(decl=decl))
            node.issue = number

        for comment in payload.comments.get(number, []):
            body = comment.get("body") or ""
            claim = parse_lease_comment(body)
            if claim is None:
                continue
            identity = Identity(login=claim.login, session=_session_of(body))
            timeline.identities.setdefault(identity.label, identity)
            if claim.action == "claim":
                claim_identity[number] = identity
                kind = EventKind.TASK_CLAIMED
            elif claim.action == "release":
                kind = EventKind.TASK_RELEASED
            else:
                continue
            timeline.events.append(
                Event(
                    at=utc(comment.get("createdAt") or ""),
                    kind=kind,
                    actor=Actor.WORKER,
                    decl=decl,
                    issue=number,
                    identity=identity,
                )
            )

        if RETRACTED_LABEL in labels and issue.get("closedAt"):
            timeline.events.append(
                Event(
                    at=utc(issue["closedAt"]),
                    kind=EventKind.RETRACTED,
                    actor=Actor.ORCHESTRATOR,
                    decl=decl,
                    issue=number,
                )
            )

def _pr_events(
    payload: Payload,
    timeline: Timeline,
    task_decl: dict[int, str],
    pr_author: dict[int, str],
    claim_identity: dict[int, Identity],
    index_of: dict[str, int],
) -> None:
    """Submissions, merges, closures, and the edges a reduction declares."""
    for pr in payload.prs:
        number = pr["number"]
        issue = issue_of_branch(pr.get("headRefName") or "") or 0
        decl = task_decl.get(issue, "")
        identity = claim_identity.get(
            issue, Identity(login=pr_author.get(number, ""))
        )
        timeline.identities.setdefault(identity.label, identity)
        # A pull request's events share the sequence space of the commit
        # it merged as, because `seq` breaks a timestamp tie and the two
        # sources would otherwise be comparing a commit index against a
        # default of zero — which put every GitHub event first.
        seq = index_of.get((pr.get("mergeCommit") or {}).get("oid") or "", 0)
        timeline.events.append(
            Event(
                at=utc(pr["createdAt"]),
                kind=EventKind.PR_OPENED,
                actor=Actor.WORKER,
                decl=decl,
                issue=issue,
                pr=number,
                identity=identity,
                seq=seq,
            )
        )
        if pr.get("mergedAt"):
            timeline.events.append(
                Event(
                    at=utc(pr["mergedAt"]),
                    kind=EventKind.PR_MERGED,
                    actor=Actor.WORKER,
                    decl=decl,
                    issue=issue,
                    pr=number,
                    identity=identity,
                    seq=seq,
                )
            )
            record = parse_reduction_block(pr.get("body") or "")
            if isinstance(record, ReductionRecord):
                for child in record.children:
                    timeline.edges.append(
                        Edge(
                            parent=record.parent,
                            child=child.decl,
                            source="reduction",
                            at=utc(pr["mergedAt"]),
                            pr=number,
                        )
                    )
                    timeline.nodes.setdefault(child.decl, Node(decl=child.decl))
                parent = timeline.nodes.setdefault(
                    record.parent, Node(decl=record.parent)
                )
                parent.declares_deps = True
                timeline.events.append(
                    Event(
                        at=utc(pr["mergedAt"]),
                        kind=EventKind.REDUCTION_ACCEPTED,
                        actor=Actor.WORKER,
                        decl=record.parent,
                        issue=issue,
                        pr=number,
                        identity=identity,
                        detail=f"{len(record.children)} obligations",
                        seq=seq,
                    )
                )
        elif pr.get("closedAt"):
            timeline.events.append(
                Event(
                    at=utc(pr["closedAt"]),
                    kind=EventKind.PR_CLOSED,
                    actor=Actor.WORKER,
                    decl=decl,
                    issue=issue,
                    pr=number,
                    identity=identity,
                    seq=seq,
                )
            )

def _commit_events(
    commits: list[CommitRecord],
    timeline: Timeline,
    by_merge: dict[str, dict[str, Any]],
    pr_author: dict[int, str],
    claim_identity: dict[int, Identity],
    names: _Names,
) -> None:
    """Declarations appearing, placeholders going away, declarations going.

    A declaration the project deleted is not one it still has. Without
    the removal the box stays on the board with whatever was last known
    about it, which reads as a result the project holds and nothing
    depends on.
    """
    for record in commits:
        pr = by_merge.get(record.sha)
        if pr is None:
            actor, pr_number, identity = Actor.ORCHESTRATOR, 0, None
            at = record.at
        else:
            pr_number = pr["number"]
            issue = issue_of_branch(pr.get("headRefName") or "") or 0
            identity = claim_identity.get(
                issue, Identity(login=pr_author.get(pr_number, ""))
            )
            actor = Actor.WORKER
            # A declaration enters the project's history when its commit
            # does. For work delivered by a pull request that is the
            # merge, not the moment the contributor wrote it — the commit
            # date can precede the merge by hours, and did, which put a
            # proof before the publication that asked for it and left the
            # node unclaimed-and-unfilled for the step in between.
            at = utc(pr.get("mergedAt") or "") or record.at

        for state in record.stated:
            decl = names.canonical(state.decl)
            node = timeline.nodes.setdefault(decl, Node(decl=decl))
            if not node.file:
                node.file = state.file
            if not node.stated_at:
                node.stated_at = at
            timeline.events.append(
                Event(
                    at=at,
                    kind=EventKind.NODE_STATED,
                    actor=actor,
                    decl=decl,
                    pr=pr_number,
                    sha=record.sha,
                    identity=identity,
                    seq=record.index,
                )
            )
            if not state.has_placeholder:
                # Introduced with no placeholder — it never passes through a
                # placeholder, so it has no `filled` event of its own.
                node.body_filled_at = at
                timeline.events.append(
                    Event(
                        at=at,
                        kind=EventKind.NODE_FILLED,
                        actor=actor,
                        decl=decl,
                        pr=pr_number,
                        sha=record.sha,
                        identity=identity,
                        detail="stated clear",
                        seq=record.index,
                    )
                )

        for scanned in record.filled:
            decl = names.canonical(scanned)
            node = timeline.nodes.setdefault(decl, Node(decl=decl))
            node.body_filled_at = at
            timeline.events.append(
                Event(
                    at=at,
                    kind=EventKind.NODE_FILLED,
                    actor=actor,
                    decl=decl,
                    pr=pr_number,
                    sha=record.sha,
                    identity=identity,
                    seq=record.index,
                    detail="placeholder filled",
                )
            )

        for scanned in record.removed:
            decl = names.canonical(scanned)
            if decl not in timeline.nodes:
                continue
            timeline.events.append(
                Event(
                    at=at,
                    kind=EventKind.NODE_REMOVED,
                    actor=actor,
                    decl=decl,
                    pr=pr_number,
                    sha=record.sha,
                    identity=identity,
                    seq=record.index,
                    detail="declaration removed",
                )
            )

        if pr is None and (record.stated or record.filled):
            timeline.events.append(
                Event(
                    at=at,
                    kind=EventKind.ORCHESTRATOR_COMMIT,
                    actor=Actor.ORCHESTRATOR,
                    sha=record.sha,
                    detail=record.subject,
                    seq=record.index,
                )
            )

def _settle_given(timeline: Timeline) -> None:
    """Keep `given` only where no commit in this project shows the work.

    The plan records most of its complete nodes for declarations the
    scan does see, and those must keep reading off their own history —
    the commit that filled them is the interesting fact, and a plan
    status is the orchestrator's word for it, which can be stale. What
    is left is the work no commit here can witness.
    """
    witnessed = {
        event.decl
        for event in timeline.events
        if event.kind in (EventKind.NODE_STATED, EventKind.NODE_FILLED)
    }
    for decl, node in timeline.nodes.items():
        if node.given and decl in witnessed:
            node.given = False


def _drop_unused_upstream(timeline: Timeline) -> None:
    """Forget a library result nothing in the project uses.

    An `upstream` node earns its place by being depended on: the library
    already proved it, so the only fact this timeline holds about it is
    which of the project's declarations reached for it and when. With no
    such edge there is no such fact, and nothing here can date it — it
    would be drawn from the first frame to the last, complete before the
    history starts, which reads as work that was already done rather than
    as the plan's note that someone else did it.

    A plan records these for two different reasons and only one of them
    is a dependency. `uses`/`proof_uses` pointing at a library lemma is
    the project leaning on it. A bare entry with no edge is usually the
    other kind — the chapter's headline theorem, recorded so the work is
    not attempted twice — and that is a fact about the plan rather than
    about this history, which is where it should be read.
    """
    used = {edge.child for edge in timeline.edges}
    dropped = sorted(
        decl
        for decl, node in timeline.nodes.items()
        if node.upstream and decl not in used
    )
    if dropped:
        gone = set(dropped)
        for decl in dropped:
            del timeline.nodes[decl]
        timeline.edges[:] = [
            edge
            for edge in timeline.edges
            if edge.parent not in gone and edge.child not in gone
        ]
        timeline.notes.append(
            f"{len(dropped)} library results the plan names are left out: nothing "
            "in the project declares a dependency on them, so this history has "
            "nothing to say about when they mattered."
        )


def _add_notes(timeline: Timeline) -> None:
    """State what the derivation could not establish."""
    # A node with no dependency of its own is a leaf, not a loose end: it
    # still hangs off whatever depends on it. Only one with no edge at
    # either end is drawn unattached, and only that is worth reporting.
    attached = {edge.parent for edge in timeline.edges}
    attached |= {edge.child for edge in timeline.edges}
    loose = sum(1 for decl in timeline.nodes if decl not in attached)
    if loose:
        timeline.notes.append(
            f"{loose} of {len(timeline.nodes)} nodes have no dependency in either "
            "direction; they are drawn unattached because nothing in the plan "
            "reaches them."
        )
    if timeline.ambiguous:
        timeline.notes.append(
            f"{len(timeline.ambiguous)} declaration names are shared by more than "
            "one plan node; those are kept apart rather than merged."
        )
    unattributed = [i for i in timeline.identities.values() if not i.distinguishable]
    if unattributed:
        timeline.notes.append(
            f"{len(unattributed)} worker identities carry no session and cannot be "
            "told apart from each other."
        )
