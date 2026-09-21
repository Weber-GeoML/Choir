"""Tests for `orchestrator.graph.sync` — module discovery and the write half."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

import orchestrator.graph.sync as sync_mod
from gate.provers.lean4 import LEAN4
from orchestrator.graph.reconcile import Reconciliation
from orchestrator.graph.sync import (
    GraphSyncError,
    project_modules,
    read_graph,
    serialize,
    sync_graph,
)


def _project(root: Path, graph: dict | None = None) -> Path:
    (root / "Proj").mkdir(parents=True)
    (root / "Proj" / "Core.lean").write_text("theorem t : True := trivial\n")
    (root / "Proj.lean").write_text("import Proj.Core\n")
    (root / "lakefile.lean").write_text("-- build script, not a module\n")
    build = root / ".lake" / "build" / "lib"
    build.mkdir(parents=True)
    (build / "Stale.lean").write_text("-- build output\n")
    if graph is not None:
        (root / "roadmap").mkdir()
        (root / "roadmap" / "graph.json").write_text(serialize(graph))
    return root


def test_modules_are_named_by_path_and_skip_build_output(tmp_path: Path) -> None:
    modules = project_modules(_project(tmp_path), LEAN4)
    assert modules == {
        "Proj": "Proj.lean",
        "Proj.Core": "Proj/Core.lean",
    }


def test_read_graph_reports_a_missing_or_broken_file(tmp_path: Path) -> None:
    with pytest.raises(GraphSyncError, match="no graph at"):
        read_graph(tmp_path)
    (tmp_path / "roadmap").mkdir()
    (tmp_path / "roadmap" / "graph.json").write_text("{not json")
    with pytest.raises(GraphSyncError, match="unreadable graph"):
        read_graph(tmp_path)


def test_read_graph_rejects_a_non_object(tmp_path: Path) -> None:
    (tmp_path / "roadmap").mkdir()
    (tmp_path / "roadmap" / "graph.json").write_text("[]")
    with pytest.raises(GraphSyncError, match="not an object"):
        read_graph(tmp_path)


def test_serialize_is_stable_and_newline_terminated() -> None:
    rendered = serialize({"target": "s", "nodes": {"a": {"kind": "theorem"}}})
    assert rendered.endswith("}\n")
    assert serialize(json.loads(rendered)) == rendered


def _stub(monkeypatch: pytest.MonkeyPatch, graph: dict) -> None:
    monkeypatch.setattr(
        sync_mod, "derive", lambda checkout, prover=None: Reconciliation(
            graph=graph, added=("helper",), rewired=("main",)
        )
    )


def test_sync_writes_the_derived_graph(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _project(tmp_path, {"target": "s", "nodes": {}})
    _stub(monkeypatch, {"target": "s", "nodes": {"helper": {"kind": "theorem"}}})
    report = sync_graph(root)
    assert report["written"] is True and report["stale"] is True
    assert report["added"] == ["helper"] and report["rewired"] == ["main"]
    assert json.loads((root / "roadmap" / "graph.json").read_text())["nodes"] == {
        "helper": {"kind": "theorem"}
    }


def test_check_reports_staleness_without_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path, {"target": "s", "nodes": {}})
    before = (root / "roadmap" / "graph.json").read_text()
    _stub(monkeypatch, {"target": "s", "nodes": {"helper": {"kind": "theorem"}}})
    report = sync_graph(root, check=True)
    assert report["stale"] is True and report["written"] is False
    assert (root / "roadmap" / "graph.json").read_text() == before


def test_a_graph_already_in_step_is_left_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = {"target": "s", "nodes": {"a": {"kind": "theorem"}}}
    root = _project(tmp_path, graph)
    _stub(monkeypatch, graph)
    report = sync_graph(root)
    assert report["stale"] is False and report["written"] is False


def test_freshness_is_checked_before_the_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale checkout imports its last built state without complaint, so
    nothing downstream could detect a graph derived from one."""
    root = _project(tmp_path, {"target": "s", "nodes": {}})
    order: list[str] = []
    monkeypatch.setattr(sync_mod, "require_built", lambda c, p: order.append("check"))
    monkeypatch.setattr(
        sync_mod, "collect_dependencies",
        lambda profile, ws, imports, watched: order.append("probe") or [],
    )
    monkeypatch.setattr(sync_mod, "read_prover", lambda c: "lean4")
    sync_mod.derive(root)
    assert order == ["check", "probe"]


def test_the_probe_watches_the_library_results_the_plan_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the watch list the probe drops every library constant, and
    an `upstream` node ends up in the plan with nothing pointing at it."""
    graph = {
        "target": "s",
        "nodes": {
            "main": {"kind": "theorem", "decl": "Proj.main"},
            "lib": {"kind": "theorem", "decl": "Lib.thm", "upstream": True},
            "other": {"kind": "theorem", "decl": "Lib.other", "upstream": True},
            "group": {"kind": "group"},
            "nameless": {"kind": "theorem", "upstream": True},
        },
    }
    root = _project(tmp_path, graph)
    seen: list[list[str]] = []
    monkeypatch.setattr(sync_mod, "require_built", lambda c, p: None)
    monkeypatch.setattr(sync_mod, "read_prover", lambda c: "lean4")
    monkeypatch.setattr(
        sync_mod, "collect_dependencies",
        lambda profile, ws, imports, watched: seen.append(watched) or [],
    )
    sync_mod.derive(root)
    assert seen == [["Lib.other", "Lib.thm"]]


def test_a_stale_checkout_refuses_rather_than_reporting(tmp_path: Path) -> None:
    profile = dataclasses.replace(LEAN4, freshness_command=("false",))
    with pytest.raises(GraphSyncError, match="not built at its current source"):
        sync_mod.require_built(tmp_path, profile)


def test_an_up_to_date_checkout_is_silent(tmp_path: Path) -> None:
    profile = dataclasses.replace(LEAN4, freshness_command=("true",))
    sync_mod.require_built(tmp_path, profile)


def test_a_prover_that_cannot_be_asked_is_not_blocked(tmp_path: Path) -> None:
    """The dependency probe refuses such a prover on its own; this check
    has nothing to add."""
    profile = dataclasses.replace(LEAN4, freshness_command=None)
    sync_mod.require_built(tmp_path, profile)
