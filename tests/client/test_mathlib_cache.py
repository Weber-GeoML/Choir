"""Tests for client.mathlib_cache — the shared Mathlib dependency store.

The store is shared by symlink, which every filesystem supports, so these tests
exercise the real mechanism rather than a portable stand-in.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from client import mathlib_cache
from client.mathlib_cache import (
    PACKAGES_REL,
    manifest_key,
    prepare_workspace_deps,
)


def _write_manifest(ws: Path, text: str = '{"packages": []}') -> None:
    (ws / "lake-manifest.json").write_text(text, encoding="utf-8")


def _make_packages(root: Path, marker: str = "olean-bytes") -> Path:
    """Create a non-empty `.lake/packages` under `root`; return the packages dir."""
    pkgs = root / PACKAGES_REL
    lib = pkgs / "mathlib" / ".lake" / "build" / "lib"
    lib.mkdir(parents=True)
    (lib / "Mathlib.olean").write_text(marker, encoding="utf-8")
    return pkgs


def _olean(root: Path) -> Path:
    return root / PACKAGES_REL / "mathlib" / ".lake" / "build" / "lib" / "Mathlib.olean"


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    """Keep the store under a temp dir, never the real `~/.choir`."""
    monkeypatch.setenv("CHOIR_MATHLIB_STORE", str(tmp_path / "store"))


# --- manifest_key ----------------------------------------------------------


def test_manifest_key_stable_and_distinct() -> None:
    # Non-JSON input falls back to raw-text hashing: stable + distinct.
    assert manifest_key("abc") == manifest_key("abc")
    assert manifest_key("abc") != manifest_key("abd")


def test_manifest_key_ignores_root_project_name() -> None:
    """Cross-project sharing: two projects whose dependency sets are identical
    but whose root package names differ must land on the SAME store key. The
    `.lake/packages` oleans depend only on the resolved deps, not on the name
    of the project consuming them."""
    deps = (
        '[{"url":"https://github.com/leanprover-community/mathlib4",'
        '"name":"mathlib","rev":"abc123"}]'
    )
    proj_a = (
        '{"version":"1.1.0","packagesDir":".lake/packages",'
        f'"packages":{deps},"name":"FormalQualBench","lakeDir":".lake"}}'
    )
    proj_b = (
        '{"version":"1.1.0","packagesDir":".lake/packages",'
        f'"packages":{deps},"name":"Project","lakeDir":".lake"}}'
    )
    assert manifest_key(proj_a) == manifest_key(proj_b)


def test_manifest_key_ignores_input_rev() -> None:
    """Same resolved `rev`, different `inputRev` (one project pins by tag, the
    other by SHA): identical oleans, so the same key."""
    by_tag = '{"packages":[{"url":"u","name":"mathlib","rev":"deadbeef","inputRev":"v4.31.0"}]}'
    by_sha = '{"packages":[{"url":"u","name":"mathlib","rev":"deadbeef","inputRev":"deadbeef"}]}'
    assert manifest_key(by_tag) == manifest_key(by_sha)


def test_manifest_key_distinct_for_different_dep_revs() -> None:
    """Guard against a false HIT: different resolved Mathlib revisions are
    different oleans and must never collide on one key."""
    one = '{"packages":[{"url":"u","name":"mathlib","rev":"aaa"}],"name":"P"}'
    two = '{"packages":[{"url":"u","name":"mathlib","rev":"bbb"}],"name":"P"}'
    assert manifest_key(one) != manifest_key(two)


# --- prepare_workspace_deps ------------------------------------------------


def test_no_manifest_is_noop(tmp_path: Path) -> None:
    assert prepare_workspace_deps(tmp_path) == "no-manifest"


def test_present_when_packages_already_there(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_manifest(ws)
    _make_packages(ws)
    assert prepare_workspace_deps(ws, cache_get=lambda w: pytest.fail("no fetch")) == "present"


def test_hit_links_to_store_without_fetching(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    manifest = '{"packages":[{"name":"mathlib","rev":"v4.31.0"}]}'
    _write_manifest(ws, manifest)
    _make_packages(mathlib_cache.store_root() / manifest_key(manifest), marker="from-store")

    def _boom(_w: Path) -> bool:
        raise AssertionError("cache_get must not run on a store hit")

    assert prepare_workspace_deps(ws, cache_get=_boom) == "hit"
    assert _olean(ws).read_text() == "from-store"  # came from the store
    # Shared by reference, not copied: one set of bytes serves every workspace.
    assert (ws / PACKAGES_REL).is_symlink()


def test_miss_fetches_then_saves_to_store(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    manifest = '{"packages":[{"name":"mathlib","rev":"v4.31.0"}]}'
    _write_manifest(ws, manifest)

    def _fetch(w: Path) -> bool:
        _make_packages(w, marker="fetched")
        return True

    assert prepare_workspace_deps(ws, cache_get=_fetch) == "fetched"
    saved = _olean(mathlib_cache.store_root() / manifest_key(manifest))
    assert saved.read_text() == "fetched"  # seeded for the next workspace
    # Moved, not copied — the workspace now reads the store's one copy.
    assert (ws / PACKAGES_REL).is_symlink()
    assert _olean(ws).read_text() == "fetched"


def test_second_workspace_hits_store_seeded_by_first(tmp_path: Path) -> None:
    manifest = '{"packages":[{"name":"mathlib","rev":"v4.31.0"}]}'

    ws1 = tmp_path / "ws1"
    ws1.mkdir()
    _write_manifest(ws1, manifest)
    prepare_workspace_deps(ws1, cache_get=lambda w: bool(_make_packages(w, "v1")))

    ws2 = tmp_path / "ws2"
    ws2.mkdir()
    _write_manifest(ws2, manifest)
    assert prepare_workspace_deps(ws2, cache_get=lambda w: pytest.fail("should hit")) == "hit"
    assert _olean(ws2).read_text() == "v1"


def test_cross_project_same_deps_shares_store(tmp_path: Path) -> None:
    """Two DIFFERENT projects (different root names) pinning the identical
    dependency set: the second workspace HITs the store the first one seeded —
    one Mathlib copy serves both projects, not one per project."""
    deps = '[{"url":"u","name":"mathlib","rev":"v4.31.0"}]'
    man_a = f'{{"packages":{deps},"name":"ProjectA"}}'
    man_b = f'{{"packages":{deps},"name":"ProjectB"}}'

    ws1 = tmp_path / "a"
    ws1.mkdir()
    _write_manifest(ws1, man_a)
    prepare_workspace_deps(ws1, cache_get=lambda w: bool(_make_packages(w, "shared")))

    ws2 = tmp_path / "b"
    ws2.mkdir()
    _write_manifest(ws2, man_b)
    assert prepare_workspace_deps(ws2, cache_get=lambda w: pytest.fail("should hit")) == "hit"
    assert _olean(ws2).read_text() == "shared"


def test_miss_fetch_fails_is_skipped(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_manifest(ws)
    assert prepare_workspace_deps(ws, cache_get=lambda w: False) == "skipped"


def test_miss_fetch_produces_no_packages_is_skipped(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_manifest(ws)
    assert prepare_workspace_deps(ws, cache_get=lambda w: True) == "skipped"


def test_fetched_nostore_keeps_deps_in_the_workspace(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """If the workspace cannot be linked at the store, the fetched dependencies
    must stay where the build can use them rather than being stranded."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_manifest(ws)
    monkeypatch.setattr(mathlib_cache, "link_store", lambda s, d: False)
    status = prepare_workspace_deps(ws, cache_get=lambda w: bool(_make_packages(w)))
    assert status == "fetched-nostore"
    assert _olean(ws).is_file()


def test_reclaimed_store_clears_the_stale_link_and_refetches(tmp_path: Path) -> None:
    """The store is reclaimable space, so a workspace can outlive its entry.
    The dangling link must not read as `present`, and must not block a refetch."""
    ws = tmp_path / "ws"
    ws.mkdir()
    manifest = '{"packages":[{"name":"mathlib","rev":"v4.31.0"}]}'
    _write_manifest(ws, manifest)
    entry = mathlib_cache.store_root() / manifest_key(manifest)
    _make_packages(entry, marker="first")
    assert prepare_workspace_deps(ws, cache_get=lambda w: pytest.fail("should hit")) == "hit"

    shutil.rmtree(entry)  # overseer reclaims space between projects
    assert (ws / PACKAGES_REL).is_symlink()  # link survives, target does not

    status = prepare_workspace_deps(ws, cache_get=lambda w: bool(_make_packages(w, "refetched")))
    assert status == "fetched"
    assert _olean(ws).read_text() == "refetched"


def test_empty_packages_dir_does_not_block_a_store_hit(tmp_path: Path) -> None:
    """An empty `.lake/packages` holds no dependencies, so it must not stop the
    workspace linking to the store — the cost of missing a hit is a full refetch."""
    ws = tmp_path / "ws"
    ws.mkdir()
    manifest = '{"packages":[{"name":"mathlib","rev":"v4.31.0"}]}'
    _write_manifest(ws, manifest)
    (ws / PACKAGES_REL).mkdir(parents=True)
    _make_packages(mathlib_cache.store_root() / manifest_key(manifest), marker="stored")

    assert prepare_workspace_deps(ws, cache_get=lambda w: pytest.fail("should hit")) == "hit"
    assert _olean(ws).read_text() == "stored"


def test_removing_a_workspace_leaves_the_store_intact(tmp_path: Path) -> None:
    """Teardown deletes the link, never what it points at — otherwise releasing
    one task would destroy the dependencies of every other workspace."""
    ws = tmp_path / "ws"
    ws.mkdir()
    manifest = '{"packages":[{"name":"mathlib","rev":"v4.31.0"}]}'
    _write_manifest(ws, manifest)
    entry = mathlib_cache.store_root() / manifest_key(manifest)
    _make_packages(entry, marker="precious")
    assert prepare_workspace_deps(ws, cache_get=lambda w: pytest.fail("should hit")) == "hit"

    shutil.rmtree(ws, ignore_errors=True)
    assert _olean(entry).read_text() == "precious"


def test_store_seeded_during_fetch_keeps_one_copy(tmp_path: Path) -> None:
    """Two workspaces can miss and fetch concurrently. The loser discards its
    own copy and links to the winner's rather than keeping a second one."""
    ws = tmp_path / "ws"
    ws.mkdir()
    manifest = '{"packages":[{"name":"mathlib","rev":"v4.31.0"}]}'
    _write_manifest(ws, manifest)
    entry = mathlib_cache.store_root() / manifest_key(manifest)

    def _fetch_while_another_workspace_seeds(w: Path) -> bool:
        _make_packages(w, marker="ours")
        _make_packages(entry, marker="theirs")
        return True

    assert prepare_workspace_deps(ws, cache_get=_fetch_while_another_workspace_seeds) == "fetched"
    assert (ws / PACKAGES_REL).is_symlink()
    assert _olean(ws).read_text() == "theirs"
    assert not (ws / ".lake" / "packages.superseded").exists()
