"""Tests for `orchestrator.graph.reconcile`.

The fold is where a plan can be damaged: it writes over edges, mints
nodes, and must leave the orchestrator's own hand-authored half exactly
as it found it.
"""

from __future__ import annotations

from typing import Any

from gate.provers.base import DeclDependency
from orchestrator.graph.reconcile import reconcile


def dep(decl: str, *, uses: tuple[str, ...] = (), proof_uses: tuple[str, ...] = (),
        kind: str = "theorem", module: str = "Proj.Core", line: int = 1,
        placeholder: bool = False) -> DeclDependency:
    return DeclDependency(
        decl=decl, kind=kind, module=module, line=line,
        has_placeholder=placeholder, uses=uses, proof_uses=proof_uses,
    )


def graph(**nodes: dict[str, Any]) -> dict[str, Any]:
    return {"target": "a source", "nodes": nodes}


FILES = {"Proj.Core": "Proj/Core.lean"}


def test_writes_the_edges_a_term_actually_has() -> None:
    before = graph(
        main={"kind": "theorem", "decl": "Proj.main", "statement": "formalized",
              "uses": [], "proof_uses": []},
        helper={"kind": "theorem", "decl": "Proj.helper", "statement": "formalized"},
    )
    result = reconcile(before, [dep("Proj.main", proof_uses=("Proj.helper",))], files=FILES)
    assert result.graph["nodes"]["main"]["proof_uses"] == ["helper"]
    assert result.rewired == ("main",)
    assert result.added == ()


def test_mints_a_node_for_a_declaration_the_plan_never_named() -> None:
    result = reconcile(
        graph(main={"kind": "theorem", "decl": "Proj.main", "statement": "formalized"}),
        [dep("Proj.main", proof_uses=("Proj.helper",)), dep("Proj.helper")],
        files=FILES,
    )
    minted = result.graph["nodes"]["helper"]
    assert minted == {
        "kind": "theorem",
        "file": "Proj/Core.lean",
        "decl": "Proj.helper",
        "statement": "formalized",
        "proof": "formalized",
        "uses": [],
        "proof_uses": [],
    }
    assert result.added == ("helper",)
    assert result.graph["nodes"]["main"]["proof_uses"] == ["helper"]


def test_a_minted_node_records_an_open_placeholder() -> None:
    result = reconcile(graph(), [dep("Proj.open", placeholder=True)], files=FILES)
    assert result.graph["nodes"]["open"]["proof"] == "planned"


def test_a_definition_is_minted_as_one() -> None:
    result = reconcile(graph(), [dep("Proj.w", kind="definition")], files=FILES)
    assert result.graph["nodes"]["w"]["kind"] == "definition"


def test_leaves_a_planned_statement_alone() -> None:
    before = graph(
        main={"kind": "theorem", "decl": "Proj.main", "statement": "planned",
              "proof_uses": ["by_hand"]},
        by_hand={"kind": "theorem", "decl": "Proj.helper", "statement": "formalized"},
    )
    result = reconcile(before, [dep("Proj.main", proof_uses=("Proj.helper",))], files=FILES)
    assert result.graph["nodes"]["main"]["proof_uses"] == ["by_hand"]
    assert result.rewired == ()


def test_leaves_an_upstream_result_and_a_group_alone() -> None:
    before = graph(
        core={"kind": "group", "doc": "core.md", "proof_uses": ["kept"]},
        lib={"kind": "theorem", "decl": "Lib.thm", "upstream": True, "uses": ["kept"]},
    )
    result = reconcile(before, [dep("Lib.thm", uses=("Proj.other",))], files=FILES)
    assert result.graph["nodes"]["lib"]["uses"] == ["kept"]
    assert result.graph["nodes"]["core"]["proof_uses"] == ["kept"]


def test_never_rewrites_a_statement_or_a_note() -> None:
    before = graph(
        main={"kind": "theorem", "parent": "core", "decl": "Proj.main", "doc": "core.md#main",
              "statement": "formalized", "proof": "formalized", "uses": [], "proof_uses": []},
    )
    result = reconcile(before, [dep("Proj.main", placeholder=True)], files=FILES)
    kept = result.graph["nodes"]["main"]
    assert kept["statement"] == "formalized"
    assert kept["parent"] == "core"
    assert kept["doc"] == "core.md#main"


def test_a_placeholder_downgrades_a_proof_the_plan_called_complete() -> None:
    before = graph(
        main={"kind": "theorem", "decl": "Proj.main", "statement": "formalized",
              "proof": "formalized", "uses": [], "proof_uses": []},
    )
    result = reconcile(before, [dep("Proj.main", placeholder=True)], files=FILES)
    assert result.graph["nodes"]["main"]["proof"] == "planned"
    assert "main" in result.rewired


def test_a_landed_proof_clears_a_status_the_plan_left_planned() -> None:
    before = graph(
        main={"kind": "theorem", "decl": "Proj.main", "statement": "formalized",
              "proof": "planned", "uses": [], "proof_uses": []},
    )
    result = reconcile(before, [dep("Proj.main")], files=FILES)
    assert result.graph["nodes"]["main"]["proof"] == "formalized"


def test_a_proof_resting_on_a_placeholder_still_reads_from_its_own_term() -> None:
    """The status is the declaration's term, not a verdict on what it rests on.

    Marking every reachable node `planned` erases the difference between
    work nobody has started and work that landed and is waiting on a
    dependency — and mid-formalization that is most of the project.
    """
    before = graph(
        root={"kind": "theorem", "decl": "Proj.root", "statement": "formalized"},
        top={"kind": "theorem", "decl": "Proj.top", "statement": "formalized"},
    )
    result = reconcile(
        before,
        [dep("Proj.root", placeholder=True), dep("Proj.top", proof_uses=("Proj.root",))],
        files=FILES,
    )
    nodes = result.graph["nodes"]
    assert nodes["root"]["proof"] == "planned"
    assert nodes["top"]["proof"] == "formalized"


def test_an_upstream_node_keeps_the_status_the_orchestrator_gave_it() -> None:
    before = graph(
        lib={"kind": "theorem", "decl": "Lib.thm", "upstream": True, "proof": "formalized"},
    )
    result = reconcile(before, [dep("Lib.thm", placeholder=True)], files=FILES)
    assert result.graph["nodes"]["lib"]["proof"] == "formalized"


def test_reports_a_reference_the_project_does_not_declare() -> None:
    result = reconcile(
        graph(main={"kind": "theorem", "decl": "Proj.main", "statement": "formalized"}),
        [dep("Proj.main", proof_uses=("Proj.main.congr_simp",))],
        files=FILES,
    )
    assert result.unresolved == ("Proj.main.congr_simp",)
    assert result.graph["nodes"]["main"]["proof_uses"] == []


def test_a_colliding_leaf_name_falls_back_to_the_whole_name() -> None:
    before = graph(w={"kind": "definition", "decl": "Proj.other.w", "statement": "formalized"})
    result = reconcile(before, [dep("Proj.w", kind="definition")], files=FILES)
    assert "Proj_w" in result.graph["nodes"]
    assert result.graph["nodes"]["Proj_w"]["decl"] == "Proj.w"


def test_a_module_with_no_known_path_still_gets_a_node() -> None:
    result = reconcile(graph(), [dep("Proj.w", module="Proj.Elsewhere")], files=FILES)
    assert "file" not in result.graph["nodes"]["w"]


def test_running_twice_changes_nothing_the_second_time() -> None:
    deps = [dep("Proj.main", proof_uses=("Proj.helper",)), dep("Proj.helper")]
    once = reconcile(graph(), deps, files=FILES)
    twice = reconcile(once.graph, deps, files=FILES)
    assert twice.graph == once.graph
    assert not twice.changed


def test_edges_are_sorted_and_deduplicated() -> None:
    result = reconcile(
        graph(),
        [dep("Proj.main", proof_uses=("Proj.b", "Proj.a", "Proj.b")), dep("Proj.a"), dep("Proj.b")],
        files=FILES,
    )
    assert result.graph["nodes"]["main"]["proof_uses"] == ["a", "b"]


def test_keeps_the_target_and_every_other_top_level_key() -> None:
    before = {"target": "a source", "nodes": {}, "notes": ["kept"]}
    result = reconcile(before, [], files=FILES)
    assert result.graph["target"] == "a source"
    assert result.graph["notes"] == ["kept"]


def test_derives_an_edge_to_an_upstream_result_the_probe_reports() -> None:
    """A library name the plan records is on the probe's watch list, so an
    edge to it is derived like any other rather than hand-authored."""
    before = graph(
        main={"kind": "theorem", "decl": "Proj.main", "statement": "formalized",
              "uses": [], "proof_uses": []},
        chebyshev={"kind": "theorem", "decl": "Lib.chebyshev", "upstream": True},
    )
    result = reconcile(
        before, [dep("Proj.main", proof_uses=("Lib.chebyshev",))], files=FILES
    )
    assert result.graph["nodes"]["main"]["proof_uses"] == ["chebyshev"]
    assert result.unresolved == ()
    assert result.added == ()
    assert result.rewired == ("main",)


def test_an_edge_to_an_upstream_result_survives_a_rewrite() -> None:
    """A plan may record a dependency no proof term shows — one the proof
    has yet to take, or one no term will ever witness. Withdrawing it is
    the overseer's call, so a rewrite that does not rediscover it keeps
    it."""
    before = graph(
        main={"kind": "theorem", "decl": "Proj.main", "statement": "formalized",
              "uses": [], "proof_uses": ["chebyshev"]},
        chebyshev={"kind": "theorem", "decl": "Lib.chebyshev", "upstream": True},
        helper={"kind": "theorem", "decl": "Proj.helper", "statement": "formalized"},
    )
    result = reconcile(before, [dep("Proj.main", proof_uses=("Proj.helper",))], files=FILES)
    assert result.graph["nodes"]["main"]["proof_uses"] == ["chebyshev", "helper"]


def test_an_open_proof_keeps_the_route_it_intends_to_take() -> None:
    """A placeholder has no proof term; `proof_uses` there is a plan, not
    a stale reading."""
    before = graph(
        main={"kind": "theorem", "decl": "Proj.main", "statement": "formalized",
              "proof": "planned", "uses": [], "proof_uses": ["route"]},
        route={"kind": "theorem", "decl": "Proj.route", "statement": "formalized"},
        w={"kind": "definition", "decl": "Proj.w", "statement": "formalized"},
    )
    result = reconcile(
        before, [dep("Proj.main", uses=("Proj.w",), placeholder=True)], files=FILES
    )
    kept = result.graph["nodes"]["main"]
    assert kept["proof_uses"] == ["route"]
    assert kept["uses"] == ["w"]


def test_a_finished_proof_does_lose_a_route_it_never_took() -> None:
    before = graph(
        main={"kind": "theorem", "decl": "Proj.main", "statement": "formalized",
              "uses": [], "proof_uses": ["abandoned"]},
        abandoned={"kind": "theorem", "decl": "Proj.abandoned", "statement": "formalized"},
        taken={"kind": "theorem", "decl": "Proj.taken", "statement": "formalized"},
    )
    result = reconcile(before, [dep("Proj.main", proof_uses=("Proj.taken",))], files=FILES)
    assert result.graph["nodes"]["main"]["proof_uses"] == ["taken"]


def test_an_axiom_is_recorded_as_one() -> None:
    """A project axiom is a trust boundary; calling it a definition hides it."""
    result = reconcile(graph(), [dep("Proj.assumed", kind="axiom")], files=FILES)
    assert result.graph["nodes"]["assumed"]["kind"] == "axiom"


def test_a_declaration_that_moved_is_relocated() -> None:
    before = graph(
        main={"kind": "theorem", "file": "Proj/Old.lean", "decl": "Proj.main",
              "statement": "formalized", "uses": [], "proof_uses": []},
    )
    result = reconcile(
        before, [dep("Proj.main", module="Proj.New")],
        files={"Proj.New": "Proj/New.lean"},
    )
    assert result.graph["nodes"]["main"]["file"] == "Proj/New.lean"
    assert result.rewired == ("main",)


def test_a_claim_the_source_contradicts_is_reported_not_acted_on() -> None:
    before = graph(
        gone={"kind": "theorem", "decl": "Proj.gone", "statement": "formalized",
              "uses": [], "proof_uses": ["kept"]},
        kept={"kind": "theorem", "decl": "Proj.kept", "statement": "formalized"},
        early={"kind": "theorem", "decl": "Proj.early", "statement": "planned"},
    )
    result = reconcile(before, [dep("Proj.kept"), dep("Proj.early")], files=FILES)
    assert result.disputed == ("early", "gone")
    # Reported only: the stale node keeps every field it had.
    assert result.graph["nodes"]["gone"]["proof_uses"] == ["kept"]
    assert result.graph["nodes"]["early"]["statement"] == "planned"
