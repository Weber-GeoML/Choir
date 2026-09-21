"""Tests for client.build_store — the project-own `.lake/build` baseline store.

This store clones rather than links, because Lake writes the project's own
`.lake/build` on every build. The clone is filesystem-dependent, so the logic
tests inject a portable real copy in its place and the command construction is
tested separately with a mocked subprocess. The coordinator's dependency step
is injected so no real `lake` runs.
"""

from __future__ import annotations

import json
import shutil
import types
from pathlib import Path

import pytest

from client import build_store
from client.build_store import (
    BUILD_REL,
    build_key,
    evict_build_store,
    prepare_workspace_build_cache,
    reflink_tree,
    restore_build_baseline,
    save_build_baseline,
)


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("CHOIR_BUILD_STORE", str(tmp_path / "bs"))

    def _portable_clone(src: Path, dst: Path) -> bool:
        if dst.exists():
            return False
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dst)
        return True

    monkeypatch.setattr(build_store, "reflink_tree", _portable_clone)


def _make_build(root: Path, marker: str = "olean") -> Path:
    bdir = root / BUILD_REL / "lib"
    bdir.mkdir(parents=True)
    (bdir / "Proj.olean").write_text(marker, encoding="utf-8")
    return root / BUILD_REL


def _olean(root: Path) -> Path:
    return root / BUILD_REL / "lib" / "Proj.olean"


# --- key + restore/save ----------------------------------------------------


def test_build_key_stable_and_distinct() -> None:
    assert build_key("c1", "leanprover/lean4:v1") == build_key("c1", "leanprover/lean4:v1")
    assert build_key("c1", "v1") != build_key("c2", "v1")
    assert build_key("c1", "v1") != build_key("c1", "v2")


def test_restore_no_key_when_commit_or_toolchain_missing(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    assert restore_build_baseline(ws, repo="a/p", commit="", toolchain="v1") == "no-key"


def test_restore_present_when_build_already_there(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    _make_build(ws)
    assert restore_build_baseline(ws, repo="a/p", commit="c1", toolchain="v1") == "present"


def test_restore_miss_when_no_baseline(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    assert restore_build_baseline(ws, repo="a/p", commit="c1", toolchain="v1") == "miss"


def test_save_empty_is_inert_for_downstream(tmp_path: Path) -> None:
    # Downstream project: .lake/build is empty at prepare-time → nothing to save.
    ws = tmp_path / "ws"
    ws.mkdir()
    assert save_build_baseline(ws, repo="a/p", commit="c1", toolchain="v1") == "empty"


def test_save_then_restore_roundtrip(tmp_path: Path) -> None:
    ws1 = tmp_path / "ws1"
    ws1.mkdir()
    _make_build(ws1, marker="from-task-1")
    assert save_build_baseline(ws1, repo="a/p", commit="c1", toolchain="v1") == "saved"

    ws2 = tmp_path / "ws2"
    ws2.mkdir()
    assert restore_build_baseline(ws2, repo="a/p", commit="c1", toolchain="v1") == "hit"
    assert _olean(ws2).read_text(encoding="utf-8") == "from-task-1"


def test_save_routes_through_module_level_reflink(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Regression guard for the default-arg footgun: save/restore must call the
    # MODULE-LEVEL reflink_tree (monkeypatchable), never a captured default.
    # Force reflink to fail (the non-reflink-FS / CI condition) and confirm the
    # status reflects it — this fails on ANY platform if the routing regresses,
    # whereas the other tests only catch it where real reflink is unavailable.
    monkeypatch.setattr(build_store, "reflink_tree", lambda s, d: False)
    ws = tmp_path / "ws"
    ws.mkdir()
    _make_build(ws)
    assert save_build_baseline(ws, repo="a/p", commit="c1", toolchain="v1") == "saved-nostore"


def test_save_present_when_baseline_already_stored(tmp_path: Path) -> None:
    ws1 = tmp_path / "ws1"
    ws1.mkdir()
    _make_build(ws1)
    save_build_baseline(ws1, repo="a/p", commit="c1", toolchain="v1")
    ws2 = tmp_path / "ws2"
    ws2.mkdir()
    _make_build(ws2)
    assert save_build_baseline(ws2, repo="a/p", commit="c1", toolchain="v1") == "present"


def test_evict_keeps_live_and_caps_count(tmp_path: Path) -> None:
    # Seed 4 baselines for the repo, keep one "live", cap at 2.
    for i, c in enumerate(["c1", "c2", "c3", "c4"]):
        ws = tmp_path / f"ws{i}"
        ws.mkdir()
        _make_build(ws, marker=c)
        save_build_baseline(ws, repo="a/p", commit=c, toolchain="v1")
    keep = {build_key("c4", "v1")}
    evicted = evict_build_store("a/p", keep_keys=keep, max_baselines=2)
    assert build_key("c4", "v1") not in evicted  # live one survives
    assert len(evicted) >= 1  # something was pruned


# --- coordinator -----------------------------------------------------------


def _lease(ws: Path, repo: str = "a/p", commit: str = "c1") -> None:
    (ws / ".choir-lease.json").write_text(
        json.dumps(
            {
                "repo": repo,
                "issue": 1,
                "branch": "choir/1-x",
                "pinned_commit": commit,
                "claimed_at": "2026-06-20T00:00:00+00:00",
                "claimed_by": "bob",
                "target_file": "Proj.lean",
                "target_decl": "Proj.t",
                "task_type": "prove",
            }
        )
        + "\n",
        encoding="utf-8",
    )


def test_coordinator_inert_for_downstream(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    ws = tmp_path / "ws"
    ws.mkdir()
    _lease(ws)
    (ws / "lean-toolchain").write_text("leanprover/lean4:v1\n", encoding="utf-8")

    # Downstream: deps fetch fills .lake/packages only; .lake/build stays empty.
    monkeypatch.setattr("client.build_store.prepare_workspace_deps", lambda w: "fetched")
    out = prepare_workspace_build_cache(ws)
    assert out == {"deps": "fetched", "build_restore": "miss", "build_save": "empty"}


def test_coordinator_restores_then_saves_for_mathlib(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Task 1 at c1: deps fetch ALSO populates .lake/build (Mathlib-as-project).
    ws1 = tmp_path / "ws1"
    ws1.mkdir()
    _lease(ws1, commit="c1")
    (ws1 / "lean-toolchain").write_text("v1\n", encoding="utf-8")

    def fake_deps_fills_build(workspace):  # type: ignore[no-untyped-def]
        _make_build(workspace, marker="mathlib-oleans")
        return "fetched"

    monkeypatch.setattr("client.build_store.prepare_workspace_deps", fake_deps_fills_build)
    out1 = prepare_workspace_build_cache(ws1)
    assert out1["build_restore"] == "miss"
    assert out1["build_save"] == "saved"

    # Task 2 at c1: build baseline restored via COW; deps store hit, no cache-get.
    ws2 = tmp_path / "ws2"
    ws2.mkdir()
    _lease(ws2, commit="c1")
    (ws2 / "lean-toolchain").write_text("v1\n", encoding="utf-8")
    monkeypatch.setattr("client.build_store.prepare_workspace_deps", lambda w: "hit")
    out2 = prepare_workspace_build_cache(ws2)
    assert out2["build_restore"] == "hit"
    assert _olean(ws2).read_text(encoding="utf-8") == "mathlib-oleans"


# --- reflink_tree command construction (mocked subprocess) -----------------


def test_reflink_tree_macos_uses_clonefile(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    seen = {}

    def fake_run(cmd, **kw):  # type: ignore[no-untyped-def]
        seen["cmd"] = cmd
        return types.SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(build_store.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(build_store.subprocess, "run", fake_run)
    src = tmp_path / "s"
    src.mkdir()
    assert reflink_tree(src, tmp_path / "d") is True
    assert seen["cmd"][:2] == ["cp", "-cR"]


def test_reflink_tree_linux_uses_reflink_always(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    seen = {}

    def fake_run(cmd, **kw):  # type: ignore[no-untyped-def]
        seen["cmd"] = cmd
        return types.SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(build_store.platform, "system", lambda: "Linux")
    monkeypatch.setattr(build_store.subprocess, "run", fake_run)
    src = tmp_path / "s"
    src.mkdir()
    assert reflink_tree(src, tmp_path / "d") is True
    assert "--reflink=always" in seen["cmd"]


def test_reflink_tree_false_on_unsupported_fs(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        build_store.subprocess,
        "run",
        lambda cmd, **kw: types.SimpleNamespace(returncode=1, stderr="not supported"),
    )
    src = tmp_path / "s"
    src.mkdir()
    assert reflink_tree(src, tmp_path / "d") is False


def test_reflink_tree_refuses_existing_dst(tmp_path) -> None:  # type: ignore[no-untyped-def]
    src = tmp_path / "s"
    src.mkdir()
    dst = tmp_path / "d"
    dst.mkdir()
    assert reflink_tree(src, dst) is False
