from __future__ import annotations

from viz.derive import derive
from viz.github import Payload
from viz.gitscan import CommitRecord, DeclState
from viz.model import Actor, EventKind

REPO = "owner/proj"

CLAIM_P8 = """Claiming.

```choir-lease
login: alice
action: claim
protocol: 8
session: e996b24b5ca9ee8e
```
"""

CLAIM_P7 = """Claiming.

```choir-lease
login: alice
action: claim
protocol: 7
```
"""

TASK_BODY = """---
choir-task-version: 1
type: prove
target_file: Proj/Core.lean
target_decl: Proj.main_bound
project_ref:
  repo: owner/proj
  commit: abc1234
  toolchain: leanprover/lean4:v4.33.0
deps: []
---

Prose.
"""

REDUCTION_BODY = """Proves the target modulo two obligations.

```choir-reduction
choir-reduction-version: 1
parent: Proj.main_bound
children:
  - decl: Proj.step_one
  - decl: Proj.step_two
```
"""


def _payload(**over) -> Payload:
    base = dict(
        repo=REPO,
        issues=[
            {
                "number": 7,
                "title": "prove Proj.main_bound",
                "body": TASK_BODY,
                "createdAt": "2026-01-01T00:00:00Z",
                "closedAt": None,
                "state": "OPEN",
                "labels": [],
                "author": {"login": "owner"},
            }
        ],
        prs=[],
        comments={7: []},
    )
    base.update(over)
    return Payload(**base)


def test_task_publication_is_the_orchestrator() -> None:
    tl = derive(_payload(), [])
    published = [e for e in tl.events if e.kind is EventKind.TASK_PUBLISHED]
    assert [e.decl for e in published] == ["Proj.main_bound"]
    assert published[0].actor is Actor.ORCHESTRATOR


def test_a_protocol_8_claim_carries_a_session() -> None:
    tl = derive(
        _payload(
            comments={
                7: [{"body": CLAIM_P8, "createdAt": "2026-01-02T00:00:00Z"}]
            }
        ),
        [],
    )
    claim = next(e for e in tl.events if e.kind is EventKind.TASK_CLAIMED)
    assert claim.identity is not None
    assert claim.identity.session == "e996b24b5ca9ee8e"
    assert claim.identity.distinguishable


def test_a_protocol_7_claim_is_not_distinguishable() -> None:
    tl = derive(
        _payload(
            comments={
                7: [{"body": CLAIM_P7, "createdAt": "2026-01-02T00:00:00Z"}]
            }
        ),
        [],
    )
    claim = next(e for e in tl.events if e.kind is EventKind.TASK_CLAIMED)
    assert claim.identity is not None
    assert claim.identity.session == ""
    assert not claim.identity.distinguishable
    assert any("cannot be told apart" in n for n in tl.notes)


def test_a_commit_with_no_pull_request_is_the_orchestrator() -> None:
    commits = [
        CommitRecord(
            sha="deadbee",
            at="2026-01-03T00:00:00Z",
            subject="prove it directly",
            author="owner",
            stated=(
                DeclState(decl="helper", file="Proj/Core.lean", has_placeholder=False),
            ),
        )
    ]
    tl = derive(_payload(), commits)
    stated = next(e for e in tl.events if e.kind is EventKind.NODE_STATED)
    assert stated.actor is Actor.ORCHESTRATOR
    assert any(e.kind is EventKind.ORCHESTRATOR_COMMIT for e in tl.events)


def test_a_commit_delivered_by_a_pull_request_belongs_to_its_author() -> None:
    """The worker who wrote it, not the orchestrator who merged it.

    Reading this backwards credits the orchestrator with every merged
    proof, which inverts the measurement the timeline exists to make.
    """
    prs = [
        {
            "number": 11,
            "title": "choir(prove)",
            "body": "",
            "headRefName": "choir/7-main-bound",
            "createdAt": "2026-01-04T00:00:00Z",
            "mergedAt": "2026-01-05T00:00:00Z",
            "closedAt": "2026-01-05T00:00:00Z",
            "state": "MERGED",
            "author": {"login": "alice"},
            "mergeCommit": {"oid": "cafe123"},
        }
    ]
    commits = [
        CommitRecord(
            sha="cafe123",
            at="2026-01-05T00:00:00Z",
            subject="fill it",
            author="owner",
            filled=("main_bound",),
        )
    ]
    tl = derive(_payload(prs=prs), commits)
    proved = next(e for e in tl.events if e.kind is EventKind.NODE_FILLED)
    assert proved.actor is Actor.WORKER
    assert proved.identity is not None
    assert proved.identity.login == "alice"
    assert not any(e.kind is EventKind.ORCHESTRATOR_COMMIT for e in tl.events)


def test_a_merged_reduction_declares_edges() -> None:
    prs = [
        {
            "number": 12,
            "title": "choir(prove)",
            "body": REDUCTION_BODY,
            "headRefName": "choir/7-main-bound",
            "createdAt": "2026-01-04T00:00:00Z",
            "mergedAt": "2026-01-06T00:00:00Z",
            "closedAt": "2026-01-06T00:00:00Z",
            "state": "MERGED",
            "author": {"login": "alice"},
            "mergeCommit": {"oid": "beef999"},
        }
    ]
    tl = derive(_payload(prs=prs), [])
    edges = [e for e in tl.edges if e.source == "reduction"]
    assert {e.child for e in edges} == {"Proj.step_one", "Proj.step_two"}
    assert all(e.parent == "Proj.main_bound" for e in edges)
    assert tl.nodes["Proj.main_bound"].declares_deps
    assert any(e.kind is EventKind.REDUCTION_ACCEPTED for e in tl.events)


def test_a_scanned_name_joins_the_plan_name_by_leaf() -> None:
    """The plan qualifies names; the scanner reports them as written.

    Without the join the same declaration appears twice — once from the
    plan and once from the corpus — and every declaration event falls
    outside the plan's scope.
    """
    graph = {
        "nodes": {
            "main_bound": {
                "kind": "theorem",
                "decl": "Proj.main_bound",
                "file": "Proj/Core.lean",
                "parent": "core",
                "uses": [],
            }
        }
    }
    commits = [
        CommitRecord(
            sha="aaa1111",
            at="2026-01-03T00:00:00Z",
            subject="state it",
            author="owner",
            stated=(
                DeclState(
                    decl="main_bound", file="Proj/Core.lean", has_placeholder=True
                ),
            ),
        )
    ]
    tl = derive(_payload(), commits, graph=graph)
    assert "Proj.main_bound" in tl.nodes
    assert "main_bound" not in tl.nodes
    assert tl.nodes["Proj.main_bound"].stated_at == "2026-01-03T00:00:00Z"


def test_an_ambiguous_leaf_is_not_merged() -> None:
    """Two plan nodes sharing a leaf must stay two nodes.

    Merging them would draw one node where the project has two, which is
    a quieter error than reporting the collision.
    """
    graph = {
        "nodes": {
            "a": {"kind": "theorem", "decl": "A.shared", "parent": "g", "uses": []},
            "b": {"kind": "theorem", "decl": "B.shared", "parent": "g", "uses": []},
        }
    }
    commits = [
        CommitRecord(
            sha="bbb2222",
            at="2026-01-03T00:00:00Z",
            subject="state it",
            author="owner",
            stated=(
                DeclState(decl="shared", file="P.lean", has_placeholder=True),
            ),
        )
    ]
    tl = derive(_payload(), commits, graph=graph)
    assert {"A.shared", "B.shared", "shared"} <= set(tl.nodes)
    assert "shared" in tl.ambiguous
    assert any("shared by more than" in n for n in tl.notes)


def test_a_retracted_task_is_recorded() -> None:
    issue = {
        "number": 7,
        "title": "prove Proj.main_bound",
        "body": TASK_BODY,
        "createdAt": "2026-01-01T00:00:00Z",
        "closedAt": "2026-01-09T00:00:00Z",
        "state": "CLOSED",
        "labels": [{"name": "choir/retracted"}],
        "author": {"login": "owner"},
    }
    tl = derive(_payload(issues=[issue]), [])
    retracted = next(e for e in tl.events if e.kind is EventKind.RETRACTED)
    assert retracted.decl == "Proj.main_bound"
    assert retracted.actor is Actor.ORCHESTRATOR


def test_events_come_back_in_order() -> None:
    tl = derive(
        _payload(
            comments={
                7: [{"body": CLAIM_P8, "createdAt": "2026-01-02T00:00:00Z"}]
            }
        ),
        [],
    )
    assert [e.at for e in tl.events] == sorted(e.at for e in tl.events)


def test_git_and_github_timestamps_land_in_one_timezone() -> None:
    """The timeline orders events by comparing the strings.

    Git reports the committer's local offset and GitHub reports UTC, so a
    mixed pair sorted by the wrong amount — on a -04:00 machine every
    git-derived event landed four hours early.
    """
    commits = [
        CommitRecord(
            sha="deadbee",
            at="2026-01-05T16:25:30-04:00",
            subject="prove it directly",
            author="owner",
            filled=("main_bound",),
        )
    ]
    tl = derive(_payload(), commits)
    proved = next(e for e in tl.events if e.kind is EventKind.NODE_FILLED)
    assert proved.at == "2026-01-05T20:25:30Z"


def test_a_proof_delivered_by_a_pull_request_is_timed_at_the_merge() -> None:
    """A declaration enters the project's history when the merge lands.

    The commit date is when the contributor wrote it, which can be hours
    earlier. Read as history it put a proof ahead of the publication that
    asked for it, and left the node unclaimed-but-unproved for the step
    between its merge and its proof — a one-frame flash of a state that
    never happened.
    """
    prs = [
        {
            "number": 10,
            "title": "choir(prove)",
            "body": "",
            "headRefName": "choir/7-main-bound",
            "createdAt": "2026-01-05T20:13:50Z",
            "mergedAt": "2026-01-05T20:25:31Z",
            "closedAt": "2026-01-05T20:25:31Z",
            "state": "MERGED",
            "author": {"login": "alice"},
            "mergeCommit": {"oid": "cafe123"},
        }
    ]
    commits = [
        CommitRecord(
            sha="cafe123",
            at="2026-01-05T14:02:00-04:00",   # written six hours earlier
            subject="fill it",
            author="owner",
            filled=("main_bound",),
        )
    ]
    tl = derive(_payload(prs=prs), commits)
    proved = next(e for e in tl.events if e.kind is EventKind.NODE_FILLED)
    merged = next(e for e in tl.events if e.kind is EventKind.PR_MERGED)
    opened = next(e for e in tl.events if e.kind is EventKind.PR_OPENED)
    assert proved.at == merged.at == "2026-01-05T20:25:31Z"
    assert opened.at < proved.at
    # and on a tie the kind decides, so the proof reads before its merge
    assert tl.events.index(proved) < tl.events.index(merged)


def test_an_orchestrator_commit_keeps_its_own_time() -> None:
    """With no pull request, the commit *is* when it entered history."""
    commits = [
        CommitRecord(
            sha="deadbee",
            at="2026-01-05T09:00:00Z",
            subject="prove it directly",
            author="owner",
            filled=("main_bound",),
        )
    ]
    tl = derive(_payload(), commits)
    proved = next(e for e in tl.events if e.kind is EventKind.NODE_FILLED)
    assert proved.at == "2026-01-05T09:00:00Z"


def test_a_leaf_is_not_reported_as_unattached() -> None:
    """A declaration with no dependency of its own still hangs off what
    needs it; only a node nothing reaches is drawn loose."""
    graph = {
        "nodes": {
            "main": {"kind": "theorem", "decl": "Proj.main", "proof_uses": ["leaf"]},
            "leaf": {"kind": "theorem", "decl": "Proj.leaf"},
            "orphan": {"kind": "theorem", "decl": "Proj.orphan"},
        }
    }
    tl = derive(_payload(), [], graph=graph)
    (note,) = [n for n in tl.notes if "unattached" in n]
    # `orphan` and the payload's own task node, never the leaf.
    assert note.startswith("2 of 4 nodes")


def test_a_deleted_declaration_stops_counting_as_one_the_project_has() -> None:
    """Without the removal the box keeps whatever was last known about it,
    which reads as a result the project holds."""
    commits = [
        CommitRecord(
            sha="aaa1", at="2026-01-01T00:00:00Z", subject="state it", author="o",
            index=1, stated=(DeclState(decl="Proj.gone", file="P.lean",
                                       has_placeholder=False),),
        ),
        CommitRecord(
            sha="aaa2", at="2026-01-02T00:00:00Z", subject="retire it", author="o",
            index=2, removed=("Proj.gone",),
        ),
    ]
    tl = derive(_payload(), commits)
    kinds = [e.kind for e in tl.events if e.decl == "Proj.gone"]
    assert EventKind.NODE_REMOVED in kinds
    assert kinds.index(EventKind.NODE_STATED) < kinds.index(EventKind.NODE_REMOVED)


def test_a_declaration_never_seen_is_not_reported_as_removed() -> None:
    commits = [
        CommitRecord(
            sha="aaa1", at="2026-01-01T00:00:00Z", subject="tidy", author="o",
            index=1, removed=("Proj.never",),
        ),
    ]
    tl = derive(_payload(), commits)
    assert not [e for e in tl.events if e.kind is EventKind.NODE_REMOVED]


def _uses_a_library_result() -> dict[str, object]:
    return {
        "nodes": {
            "main": {"kind": "theorem", "decl": "Proj.main", "uses": ["lib"]},
            "lib": {"kind": "theorem", "decl": "Lib.thm", "upstream": True},
        }
    }


def test_a_library_result_counts_as_complete() -> None:
    """Nothing in this project's history fills it, so without this
    everything resting on it reads as unfinished forever."""
    tl = derive(_payload(), [], graph=_uses_a_library_result())
    assert tl.nodes["Lib.thm"].given
    assert tl.nodes["Lib.thm"].upstream
    assert not any("library results" in note for note in tl.notes)


def test_a_declaration_the_project_writes_is_not_upstream() -> None:
    """`given` has a second cause — a declaration the prover named itself
    — and that one is the project's own, with a history to read."""
    graph = {
        "nodes": {
            "inst": {"kind": "theorem", "decl": "Proj.inst", "proof": "formalized"},
        }
    }
    tl = derive(_payload(), [], graph=graph)
    assert tl.nodes["Proj.inst"].given
    assert not tl.nodes["Proj.inst"].upstream


def test_a_library_result_nothing_uses_is_left_out() -> None:
    """With no edge there is no fact about it in this history, and it
    would be drawn complete from the first frame to the last."""
    graph = {"nodes": {"lib": {"kind": "theorem", "decl": "Lib.thm", "upstream": True}}}
    tl = derive(_payload(), [], graph=graph)
    assert "Lib.thm" not in tl.nodes
    assert any("library results" in note for note in tl.notes)


def test_dropping_a_library_result_takes_its_edges_with_it() -> None:
    """A node the scene never draws must not leave an edge pointing at it."""
    graph = {
        "nodes": {
            "main": {"kind": "theorem", "decl": "Proj.main", "uses": ["lib"]},
            "lib": {"kind": "theorem", "decl": "Lib.thm", "upstream": True},
            "lone": {"kind": "theorem", "decl": "Lib.lone", "upstream": True},
        }
    }
    tl = derive(_payload(), [], graph=graph)
    assert {e.child for e in tl.edges} == {"Lib.thm"}
    assert all(e.parent != "Lib.lone" and e.child != "Lib.lone" for e in tl.edges)


def test_a_declaration_the_commits_show_reads_off_its_own_history() -> None:
    """A plan status is the orchestrator's word and can be stale; the
    commit that filled it is the fact."""
    graph = {
        "nodes": {
            "main": {"kind": "theorem", "decl": "Proj.main",
                     "statement": "formalized", "proof": "formalized"},
        }
    }
    commits = [
        CommitRecord(
            sha="aaa1", at="2026-01-01T00:00:00Z", subject="state it", author="o",
            index=1, stated=(DeclState(decl="Proj.main", file="P.lean",
                                       has_placeholder=True),),
        ),
    ]
    tl = derive(_payload(), commits, graph=graph)
    assert not tl.nodes["Proj.main"].given


def test_a_declaration_the_prover_named_itself_counts_as_complete() -> None:
    """An anonymous `instance` leaves the text scan no name to follow, so
    no commit can ever be found to have filled it."""
    graph = {
        "nodes": {
            "inst": {"kind": "definition", "decl": "Proj.instFooBar",
                     "statement": "formalized", "proof": "formalized"},
        }
    }
    tl = derive(_payload(), [], graph=graph)
    assert tl.nodes["Proj.instFooBar"].given


def test_an_open_proof_is_never_given() -> None:
    graph = {
        "nodes": {
            "open": {"kind": "theorem", "decl": "Proj.open",
                     "statement": "formalized", "proof": "planned"},
        }
    }
    tl = derive(_payload(), [], graph=graph)
    assert not tl.nodes["Proj.open"].given
