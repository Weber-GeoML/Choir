"""Fold a probe's dependency report into a project's `roadmap/graph.json`. Pure.

The plan's edges are a claim about the project's own declarations, and
the built environment already knows the answer, so they are derived
rather than maintained. `reconcile` takes the graph as parsed JSON and
the probe's `DeclDependency` records and returns the graph it should
be, leaving the file, the probe and the clock to the caller.

**What it writes.** Every declaration the project's source declares gets
a node, and every node whose declaration is formalized gets its `uses`
and `proof_uses` rewritten from the environment. A helper proved inside
someone else's pull request is a declaration like any other: it is the
thing later work reuses, and a plan that omits it draws the theorem
above it as resting on nothing.

**What it will not touch.** Anything a term cannot answer for. A node
whose `statement` is still `planned`, one marked `upstream`, and a
`group` are left exactly as they are: there is no term to read, and
whatever is written there is the orchestrator's intent, which a tool that
cannot see it would simply erase.

Two narrower cases of the same rule, and they are the ones easy to get
wrong:

- **Edges to `upstream` nodes are derived, and hand-written ones
  stay.** A proof term reaches hundreds of library constants and an
  edge to each would bury the plan, so the probe reports none of them
  by default — but it is given the declarations the plan's `upstream`
  nodes name, and reports those. The derived edges are therefore
  bounded by the plan and cannot enlarge it. An edge already written to
  an upstream node is kept on top of them: the probe reads proof terms,
  and a plan may record a dependency no term has yet, or one no term
  will ever show, which is the overseer's claim to withdraw and not
  this function's.
- **`proof_uses` is derived only once the proof exists.** A declaration
  still carrying a placeholder has no proof term, so its `proof_uses` is
  not a stale reading of one — it is the route the orchestrator intends
  to take, written down before the proof that will justify it. `uses`
  has no such problem: the statement's type is there either way.

`statement` on an existing node is the orchestrator's to write when work
lands, so it is read here and never rewritten. `proof` is derived, since a
status written when the task was published says nothing about whether the
proof has since landed.

**It is the declaration's own term, not a verdict on its dependencies.** A
proof resting on an open obligation still reads `formalized` here, because
the alternative does not scale: mid-formalization a project holds hundreds
of placeholders, nearly every node reaches one, and marking them all
`planned` erases the difference between work nobody has started and work
that finished and is waiting — which is the distinction the next batch of
tasks is chosen on. Whether a *particular* dependency is unconditional is a
question about one declaration, asked when something is about to be
published, and the prover answers it directly.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from gate.provers.base import DeclDependency

DERIVED_FIELDS = ("uses", "proof_uses")


@dataclass(frozen=True)
class Reconciliation:
    """The graph as it should be, and what changed to get there."""

    graph: dict[str, Any]
    added: tuple[str, ...] = ()
    rewired: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()
    disputed: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.added or self.rewired)

    def summary(self) -> dict[str, Any]:
        return {
            "added": list(self.added),
            "rewired": list(self.rewired),
            "unresolved": list(self.unresolved),
            "disputed": list(self.disputed),
            "changed": self.changed,
        }


def _derivable_fields(node: Mapping[str, Any], has_placeholder: bool) -> tuple[str, ...]:
    """Which of a node's edge lists this run is entitled to rewrite."""
    if (
        node.get("kind") == "group"
        or node.get("upstream")
        or node.get("statement") == "planned"
    ):
        return ()
    return ("uses",) if has_placeholder else DERIVED_FIELDS


def _settle_proofs(
    nodes: dict[str, dict[str, Any]],
    id_of: Mapping[str, str],
    deps: Sequence[DeclDependency],
) -> list[str]:
    """Write each derivable node's `proof` from the term the prover saw.

    A status written when the task was published stays written otherwise,
    so a declaration proved weeks ago can still read `planned`. Returns the
    nodes whose `proof` changed.
    """
    rewritten = []
    for dep in deps:
        node_id = id_of.get(dep.decl)
        if node_id is None or not _derivable_fields(nodes[node_id], dep.has_placeholder):
            continue
        settled = "planned" if dep.has_placeholder else "formalized"
        if nodes[node_id].get("proof") != settled:
            nodes[node_id]["proof"] = settled
            rewritten.append(node_id)
    return rewritten


def _node_id(decl: str, taken: Iterable[str]) -> str:
    """A readable id for a declaration that has no node yet.

    The last segment reads the way the rest of the file's ids do
    (`weight_nonneg`, not `Project.weight_nonneg`). Two declarations can
    share one last segment across namespaces, so a collision falls back
    to the whole name — ugly, unique, and stable across runs, which
    matters more here than looking nice.
    """
    seen = set(taken)
    leaf = decl.rsplit(".", 1)[-1]
    if leaf and leaf not in seen:
        return leaf
    qualified = decl.replace(".", "_")
    if qualified not in seen:
        return qualified
    suffix = 2
    while f"{qualified}_{suffix}" in seen:
        suffix += 1
    return f"{qualified}_{suffix}"


def _mint(
    nodes: dict[str, dict[str, Any]],
    id_of: dict[str, str],
    deps: Sequence[DeclDependency],
    paths: Mapping[str, str],
) -> list[str]:
    """Give every declaration with no node one, and say which were added."""
    added: list[str] = []
    for dep in sorted(deps, key=lambda d: d.decl):
        if dep.decl in id_of:
            continue
        node_id = _node_id(dep.decl, nodes)
        node: dict[str, Any] = {"kind": dep.kind}
        source = paths.get(dep.module)
        if source:
            node["file"] = source
        node["decl"] = dep.decl
        node["statement"] = "formalized"
        node["proof"] = "planned" if dep.has_placeholder else "formalized"
        node["uses"] = []
        node["proof_uses"] = []
        nodes[node_id] = node
        id_of[dep.decl] = node_id
        added.append(node_id)
    return added


def _disputed(nodes: Mapping[str, Mapping[str, Any]], present: set[str]) -> tuple[str, ...]:
    """Nodes whose existence claim the environment contradicts.

    A node recorded `formalized` whose declaration the source no longer
    has, or one still recorded `planned` that the source does have. Both
    are reported and neither is acted on: which way to correct it — edit
    the plan or restore the declaration — is a judgement about intent,
    and a tool that guessed would be changing the roadmap rather than
    reflecting it.
    """
    return tuple(sorted(
        node_id
        for node_id, spec in nodes.items()
        if spec.get("decl")
        and not spec.get("upstream")
        and spec.get("kind") != "group"
        and (spec["decl"] in present) != (spec.get("statement") != "planned")
    ))


def reconcile(
    graph: Mapping[str, Any],
    deps: Sequence[DeclDependency],
    *,
    files: Mapping[str, str] | None = None,
) -> Reconciliation:
    """Return `graph` with derived nodes and edges brought up to date.

    Every derivable node ends up carrying both edge fields, empty
    lists included: a plan where an absent field and an empty one both
    appear cannot be read as "these are the edges this term has".

    `files` maps a module name to its repo-relative source path; a module
    missing from it yields a node with no `file`, which costs the page a
    link and blocks nothing. Declarations referenced by a proof but not
    declared by the project's own source — a prover's generated
    congruence and equation lemmas — are reported as `unresolved` and
    left out: the graph holds declarations the project wrote.
    """
    out: dict[str, Any] = {k: v for k, v in graph.items() if k != "nodes"}
    nodes: dict[str, dict[str, Any]] = {
        node_id: dict(spec) for node_id, spec in (graph.get("nodes") or {}).items()
    }
    out["nodes"] = nodes
    paths = dict(files or {})

    id_of: dict[str, str] = {}
    for node_id, spec in nodes.items():
        decl = spec.get("decl")
        if decl and decl not in id_of:
            id_of[decl] = node_id

    added = _mint(nodes, id_of, deps, paths)
    minted = set(added)

    upstream = {
        node_id for node_id, spec in nodes.items() if spec.get("upstream")
    }

    rewired: list[str] = []
    unresolved: set[str] = set()
    for dep in deps:
        node_id = id_of.get(dep.decl)
        if node_id is None:
            continue
        node = nodes[node_id]
        fields = _derivable_fields(node, dep.has_placeholder)
        if not fields:
            continue
        source = paths.get(dep.module)
        located = {"kind": dep.kind} | ({"file": source} if source else {})
        if any(node.get(k) != v for k, v in located.items()):
            node.update(located)
            if node_id not in minted:
                rewired.append(node_id)
        wanted: dict[str, list[str]] = {}
        for field_name, targets in zip(DERIVED_FIELDS, (dep.uses, dep.proof_uses), strict=True):
            if field_name not in fields:
                continue
            resolved = {
                target_id
                for target_id in (id_of.get(target) for target in targets)
                if target_id is not None
            }
            unresolved.update(t for t in targets if t not in id_of)
            kept = {t for t in node.get(field_name) or [] if t in upstream}
            wanted[field_name] = sorted(resolved | kept)
        if all(node.get(f) == wanted[f] for f in wanted):
            continue
        node.update(wanted)
        if node_id not in minted:
            rewired.append(node_id)

    rewired.extend(
        node_id
        for node_id in _settle_proofs(nodes, id_of, deps)
        if node_id not in minted
    )

    return Reconciliation(
        graph=out,
        added=tuple(added),
        rewired=tuple(sorted(set(rewired))),
        unresolved=tuple(sorted(unresolved)),
        disputed=_disputed(nodes, {dep.decl for dep in deps}),
    )
