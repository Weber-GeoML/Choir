"""Layered positions for the finished proof tree.

Used for one job: giving each declaration a depth, so events that share a
timestamp can be ordered root-first. Display positions are not these —
the page recomputes the layout of whatever subgraph exists at each step,
because a fixed layout needs the future and so cannot show a project that
is still running.

An edge means `child` is needed by `parent`, so a goal nobody depends on
is a root and sits at layer 0, with its obligations below it.

Declared edges are hand-written, so nothing guarantees they are acyclic.
Layering therefore peels the acyclic part with Kahn's algorithm and
reports whatever remains rather than assigning it a depth — a node in a
cycle has no meaningful longest path, and inventing one put nodes tens of
thousands of pixels off-canvas.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from viz.model import Edge, Node

X_STEP = 190
Y_STEP = 96
ROW_MAX = 26
"""Nodes per drawn row. A layer wider than this wraps onto sub-rows;
a project's roots are often numerous, and one 25,000px row is unreadable
however far you can pan."""


@dataclass(frozen=True)
class Placed:
    decl: str
    x: int
    y: int
    layer: int


@dataclass
class Layout:
    placed: list[Placed] = field(default_factory=list)
    cycle: list[str] = field(default_factory=list)
    """Nodes the declared edges put in a cycle, drawn in their own band."""


def _layer_dag(
    nodes: dict[str, Node], edges: list[Edge]
) -> tuple[dict[str, int], list[str]]:
    """Longest-path layers for the acyclic part; the rest is the cycle set.

    Kahn's algorithm on parent→child edges. A node is emitted once every
    parent that needs it has been emitted, so its layer is one below the
    deepest of them. Whatever never becomes emittable sits in a cycle.
    """
    children: dict[str, list[str]] = {n: [] for n in nodes}
    indeg: dict[str, int] = dict.fromkeys(nodes, 0)
    for edge in edges:
        if edge.parent in nodes and edge.child in nodes:
            children[edge.parent].append(edge.child)
            indeg[edge.child] += 1

    layer = dict.fromkeys(nodes, 0)
    queue = [n for n, d in indeg.items() if d == 0]
    emitted: set[str] = set()
    while queue:
        node = queue.pop()
        emitted.add(node)
        for kid in children[node]:
            layer[kid] = max(layer[kid], layer[node] + 1)
            indeg[kid] -= 1
            if indeg[kid] == 0:
                queue.append(kid)

    cycle = sorted(n for n in nodes if n not in emitted)
    for node in cycle:
        layer.pop(node, None)
    return layer, cycle


def _rows(
    order: list[str], nodes: dict[str, Node], first_y: int
) -> list[tuple[str, int, int]]:
    """Wrap one layer's nodes onto as many rows as it needs."""
    out: list[tuple[str, int, int]] = []
    ordered = sorted(order, key=lambda d: (nodes[d].group, d))
    for start in range(0, len(ordered), ROW_MAX):
        chunk = ordered[start : start + ROW_MAX]
        offset = -(len(chunk) - 1) * X_STEP // 2
        y = first_y + (start // ROW_MAX) * (Y_STEP // 2)
        for index, decl in enumerate(chunk):
            out.append((decl, offset + index * X_STEP, y))
    return out


def place(nodes: dict[str, Node], edges: list[Edge]) -> Layout:
    """Assign every node a stable position.

    Within a layer, nodes are ordered by group then declaration name, so
    the same input always yields the same picture and related results sit
    together.
    """
    layer, cycle = _layer_dag(nodes, edges)
    by_layer: dict[int, list[str]] = {}
    for decl, lay in layer.items():
        by_layer.setdefault(lay, []).append(decl)

    result = Layout(cycle=cycle)
    y = 0
    for lay in sorted(by_layer):
        rows = _rows(by_layer[lay], nodes, y)
        for decl, x, row_y in rows:
            result.placed.append(Placed(decl=decl, x=x, y=row_y, layer=lay))
        y = max(row_y for _, _, row_y in rows) + Y_STEP

    if cycle:
        for decl, x, row_y in _rows(cycle, nodes, y + Y_STEP):
            result.placed.append(Placed(decl=decl, x=x, y=row_y, layer=-1))
    return result
