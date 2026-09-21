"""Regenerate a project's roadmap graph from its built source.

The I/O half of `orchestrator.graph`: find the project's modules, run the
prover's dependency probe over them, fold the result into
`roadmap/graph.json` with `reconcile`, and write the file back.

Run it once a pass, on the checkout you pulled and rebuilt after the
pass's merges landed. It refuses unless the artifacts are up to date with
the source: the probe reads compiled artifacts, and importing a module
whose source has since changed silently yields its last built state, so a
graph derived from a stale checkout describes an earlier commit and
nothing downstream can tell. It asks the build tool rather than building,
since a reader must not rebuild what it reads. `check=True` answers
"would this change anything?" without writing, which is the form a
scheduled job on the default branch wants: the regeneration is
idempotent, so a non-empty answer means a merge landed without one.

Only the orchestrator runs this. The gate verifies pull requests and
never writes to a checkout, and a contributor's branch has no reason to
carry a plan edit — the graph is the overseer's map of the project, kept
on the branch only the orchestrator pushes to.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from gate.provers import get_profile
from gate.provers.base import ProverProfile
from gate.provers.deps import collect_dependencies
from gate.provers.select import read_prover
from orchestrator.graph.reconcile import Reconciliation, reconcile

GRAPH_RELATIVE_PATH = Path("roadmap") / "graph.json"


class GraphSyncError(RuntimeError):
    """The graph could not be read, derived, or written."""


def project_modules(checkout: Path, profile: ProverProfile) -> dict[str, str]:
    """Map each of the project's own modules to its repo-relative source path.

    One module per source file, named by its path with separators as
    dots — the convention the build tool already imposes, and the one the
    probe's `imports` are written in. Build output and dotted directories
    are skipped, as are the prover's `protected_files`: a lakefile is a
    source file of the build, not a module of the library, and importing
    it fails.
    """
    modules: dict[str, str] = {}
    for extension in profile.file_extensions:
        for path in sorted(checkout.rglob(f"*{extension}")):
            relative = path.relative_to(checkout)
            if any(part.startswith(".") for part in relative.parts):
                continue
            if relative.as_posix() in profile.protected_files:
                continue
            module = ".".join(relative.with_suffix("").parts)
            modules[module] = relative.as_posix()
    return modules


def read_graph(checkout: Path) -> dict[str, Any]:
    path = checkout / GRAPH_RELATIVE_PATH
    try:
        graph = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise GraphSyncError(f"no graph at {GRAPH_RELATIVE_PATH}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise GraphSyncError(f"unreadable graph at {GRAPH_RELATIVE_PATH}: {exc}") from exc
    if not isinstance(graph, dict):
        raise GraphSyncError(f"{GRAPH_RELATIVE_PATH} is not an object")
    return graph


def serialize(graph: dict[str, Any]) -> str:
    """The graph as the file holds it.

    Byte-stable for a given graph, so `check` can compare rendered text
    and a run that changes nothing leaves the file untouched.
    """
    return json.dumps(graph, indent=2, ensure_ascii=False) + "\n"


def require_built(checkout: Path, profile: ProverProfile) -> None:
    """Refuse unless the checkout's artifacts are up to date with its source.

    The probe reads compiled artifacts, and importing a module whose
    source has since changed yields its last built state — silently, so
    a probe over a stale checkout reports a project the source no longer
    describes and the graph it writes describes an earlier commit. No
    reading detects that afterwards.

    The build tool's own up-to-date query answers it, without building:
    a reader must not quietly rebuild what it is reading, and a `check`
    run promises to write nothing at all. Building is the caller's to
    do, and by this point in a pass they have.
    """
    if profile.freshness_command is None:
        return
    result = subprocess.run(
        profile.freshness_command, cwd=checkout, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise GraphSyncError(
            f"{checkout} is not built at its current source "
            f"({' '.join(profile.freshness_command)} exited {result.returncode}) — "
            f"run {' '.join(profile.build_command)} first"
        )


def upstream_decls(graph: dict[str, Any]) -> list[str]:
    """The library declarations the plan names, for the probe to watch.

    An `upstream` node is a declaration someone else's library already
    proves — the plan records it so the project knows not to prove it
    again. The probe reports only the project's own declarations, so
    without this list an edge to one of these is invisible and the node
    hangs in the plan with nothing pointing at it. Passing the names the
    plan already chose keeps the derived edges bounded by the plan: a
    library lemma nobody wrote down stays out.
    """
    nodes = graph.get("nodes") or {}
    return sorted(
        {
            spec["decl"]
            for spec in nodes.values()
            if spec.get("upstream") and spec.get("decl")
        }
    )


def derive(checkout: Path, *, prover: str | None = None) -> Reconciliation:
    """Probe the built checkout and return the graph it implies. No writes."""
    profile = get_profile(prover or read_prover(checkout))
    modules = project_modules(checkout, profile)
    if not modules:
        raise GraphSyncError(f"no {profile.name} source found under {checkout}")
    require_built(checkout, profile)
    graph = read_graph(checkout)
    deps = collect_dependencies(
        profile, checkout, sorted(modules), upstream_decls(graph)
    )
    return reconcile(graph, deps, files=modules)


def sync_graph(checkout: Path, *, check: bool = False, prover: str | None = None) -> dict[str, Any]:
    """Regenerate the graph; write it unless `check`. Returns what changed."""
    result = derive(checkout, prover=prover)
    path = checkout / GRAPH_RELATIVE_PATH
    rendered = serialize(result.graph)
    stale = rendered != path.read_text(encoding="utf-8")
    if stale and not check:
        path.write_text(rendered, encoding="utf-8")
    return {
        "graph": GRAPH_RELATIVE_PATH.as_posix(),
        "checked": check,
        "written": stale and not check,
        "stale": stale,
        **result.summary(),
    }
