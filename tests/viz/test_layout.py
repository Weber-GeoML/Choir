from __future__ import annotations

from viz.layout import ROW_MAX, X_STEP, place
from viz.model import Edge, Node


def _nodes(*decls: str) -> dict[str, Node]:
    return {d: Node(decl=d) for d in decls}


def test_a_chain_layers_top_down() -> None:
    nodes = _nodes("goal", "mid", "leaf")
    edges = [
        Edge(parent="goal", child="mid", source="graph.json"),
        Edge(parent="mid", child="leaf", source="graph.json"),
    ]
    layers = {p.decl: p.layer for p in place(nodes, edges).placed}
    assert layers == {"goal": 0, "mid": 1, "leaf": 2}


def test_longest_path_wins_over_shortest() -> None:
    """A node sits below the deepest parent that needs it, not the first."""
    nodes = _nodes("goal", "mid", "shared")
    edges = [
        Edge(parent="goal", child="shared", source="graph.json"),
        Edge(parent="goal", child="mid", source="graph.json"),
        Edge(parent="mid", child="shared", source="graph.json"),
    ]
    layers = {p.decl: p.layer for p in place(nodes, edges).placed}
    assert layers["shared"] == 2


def test_a_cycle_is_reported_and_banded_not_given_a_depth() -> None:
    """Declared edges are hand-written and can cycle.

    A cycle has no longest path; relaxing until it settles put nodes tens
    of thousands of pixels off-canvas, so the cycle is peeled off and
    named instead.
    """
    nodes = _nodes("goal", "a", "b")
    edges = [
        Edge(parent="goal", child="a", source="graph.json"),
        Edge(parent="a", child="b", source="graph.json"),
        Edge(parent="b", child="a", source="graph.json"),
    ]
    layout = place(nodes, edges)
    assert layout.cycle == ["a", "b"]
    banded = {p.decl: p for p in layout.placed if p.layer == -1}
    assert set(banded) == {"a", "b"}
    assert max(p.y for p in layout.placed) < 10_000


def test_every_node_is_placed_exactly_once() -> None:
    nodes = _nodes("goal", "a", "b", "orphan")
    edges = [
        Edge(parent="goal", child="a", source="graph.json"),
        Edge(parent="a", child="b", source="graph.json"),
        Edge(parent="b", child="a", source="graph.json"),
    ]
    placed = [p.decl for p in place(nodes, edges).placed]
    assert sorted(placed) == sorted(nodes)
    assert len(placed) == len(set(placed))


def test_a_wide_layer_wraps_instead_of_running_off_canvas() -> None:
    nodes = _nodes(*[f"n{i:03d}" for i in range(ROW_MAX * 3)])
    layout = place(nodes, [])
    rows = {p.y for p in layout.placed}
    assert len(rows) == 3
    width = max(p.x for p in layout.placed) - min(p.x for p in layout.placed)
    assert width <= ROW_MAX * X_STEP


def test_positions_are_deterministic() -> None:
    """The same input must give the same picture, twice."""
    nodes = _nodes("goal", "a", "b", "c")
    edges = [Edge(parent="goal", child="a", source="graph.json")]
    first = [(p.decl, p.x, p.y) for p in place(nodes, edges).placed]
    second = [(p.decl, p.x, p.y) for p in place(nodes, edges).placed]
    assert first == second


def test_an_edge_to_an_absent_node_is_ignored() -> None:
    """Scope filtering can drop one end of a declared edge."""
    nodes = _nodes("goal")
    edges = [Edge(parent="goal", child="filtered-out", source="graph.json")]
    layout = place(nodes, edges)
    assert [p.decl for p in layout.placed] == ["goal"]
    assert layout.cycle == []
