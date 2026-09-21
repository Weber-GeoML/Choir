"""Tests for `gate.verify.statement_equiv_cli.detect_rename`.

The full CLI path requires gh + git against a real repo and is
integration-tested via the demo. These tests cover the pure-ish
rename-detection logic by running real git in a tmp repo.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gate.verify.statement_equiv_cli import detect_rename, fetch_base_contents


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return result.stdout


def _init_repo_with_rename(tmp_path: Path) -> tuple[str, str]:
    """Set up: base SHA with Old.lean, head SHA with Old.lean renamed to New.lean.

    Returns (base_sha, head_sha).
    """
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "Old.lean").write_text("theorem foo : 1 = 1 := rfl\n", encoding="utf-8")
    _git(tmp_path, "add", "Old.lean")
    _git(tmp_path, "commit", "-q", "-m", "base: add Old.lean")
    base_sha = _git(tmp_path, "rev-parse", "HEAD").strip()

    _git(tmp_path, "mv", "Old.lean", "New.lean")
    _git(tmp_path, "commit", "-q", "-m", "rename Old.lean → New.lean")
    head_sha = _git(tmp_path, "rev-parse", "HEAD").strip()

    return base_sha, head_sha


def test_detect_rename_finds_old_name(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    base_sha, head_sha = _init_repo_with_rename(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert detect_rename(base_sha, head_sha, "New.lean") == "Old.lean"


def test_detect_rename_no_rename_returns_none(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Repo with no rename — file simply added in head.
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "X.lean").write_text("-- placeholder\n", encoding="utf-8")
    _git(tmp_path, "add", "X.lean")
    _git(tmp_path, "commit", "-q", "-m", "base")
    base_sha = _git(tmp_path, "rev-parse", "HEAD").strip()
    (tmp_path / "Y.lean").write_text("theorem foo : T := rfl\n", encoding="utf-8")
    _git(tmp_path, "add", "Y.lean")
    _git(tmp_path, "commit", "-q", "-m", "add Y.lean")
    head_sha = _git(tmp_path, "rev-parse", "HEAD").strip()

    monkeypatch.chdir(tmp_path)
    assert detect_rename(base_sha, head_sha, "Y.lean") is None


def test_fetch_base_contents_returns_content_when_no_rename(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    # File exists at base under the same name — direct git show works.
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "F.lean").write_text("theorem foo : 1 = 1 := rfl\n", encoding="utf-8")
    _git(tmp_path, "add", "F.lean")
    _git(tmp_path, "commit", "-q", "-m", "base")
    base_sha = _git(tmp_path, "rev-parse", "HEAD").strip()
    (tmp_path / "F.lean").write_text("theorem foo : 1 = 1 := by rfl\n", encoding="utf-8")
    _git(tmp_path, "commit", "-q", "-am", "edit")
    head_sha = _git(tmp_path, "rev-parse", "HEAD").strip()

    monkeypatch.chdir(tmp_path)
    contents, renamed = fetch_base_contents(base_sha, head_sha, "F.lean")
    assert "theorem foo" in contents
    assert renamed is None


def test_fetch_base_contents_follows_rename(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    base_sha, head_sha = _init_repo_with_rename(tmp_path)
    monkeypatch.chdir(tmp_path)
    contents, renamed = fetch_base_contents(base_sha, head_sha, "New.lean")
    # Before the fix: contents="" (file didn't exist at base under that name).
    # After: contents loaded from Old.lean at base.
    assert "theorem foo" in contents
    assert renamed == "Old.lean"


def test_fetch_base_contents_returns_empty_for_truly_new_file(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    # File didn't exist at base under any name — not a rename.
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "X.lean").write_text("-- placeholder\n", encoding="utf-8")
    _git(tmp_path, "add", "X.lean")
    _git(tmp_path, "commit", "-q", "-m", "base")
    base_sha = _git(tmp_path, "rev-parse", "HEAD").strip()
    (tmp_path / "Brand_new.lean").write_text(
        "theorem foo : T := rfl\n", encoding="utf-8"
    )
    _git(tmp_path, "add", "Brand_new.lean")
    _git(tmp_path, "commit", "-q", "-m", "add new")
    head_sha = _git(tmp_path, "rev-parse", "HEAD").strip()

    monkeypatch.chdir(tmp_path)
    contents, renamed = fetch_base_contents(base_sha, head_sha, "Brand_new.lean")
    assert contents == ""
    assert renamed is None


@pytest.fixture
def _ensure_git_available() -> None:
    """Skip rename tests if git isn't installed in the environment."""
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        pytest.skip("git not available")
