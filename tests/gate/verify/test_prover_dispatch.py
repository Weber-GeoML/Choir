"""Tests for `gate.verify.prover_dispatch.resolve_prover_profile`.

Mirrors the tmp-git-repo pattern in `tests/gate/provers/test_select.py` for
the base-SHA reader; pins the CLI precedence rule (design note 12 §2.3):
`--prover` flag > base-SHA `.choir/project.toml` > lean4 default.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gate.provers import ProverError
from gate.provers.isabelle import ISABELLE
from gate.provers.lean4 import LEAN4
from gate.provers.rocq import ROCQ
from gate.verify.prover_dispatch import resolve_prover_profile


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return result.stdout


def _init_repo(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")


def _commit_project_toml(tmp_path: Path, prover: str, message: str) -> str:
    (tmp_path / ".choir").mkdir(exist_ok=True)
    (tmp_path / ".choir" / "project.toml").write_text(
        f'[project]\nprover = "{prover}"\n', encoding="utf-8"
    )
    _git(tmp_path, "add", ".choir/project.toml")
    _git(tmp_path, "commit", "-q", "-m", message)
    return _git(tmp_path, "rev-parse", "HEAD").strip()


def test_flag_overrides_base_sha_config(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    base_sha = _commit_project_toml(tmp_path, "rocq", "base: rocq")
    assert resolve_prover_profile("isabelle", base_sha, cwd=tmp_path) is ISABELLE


def test_base_sha_config_used_when_no_flag(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    base_sha = _commit_project_toml(tmp_path, "isabelle", "base: isabelle")
    assert resolve_prover_profile(None, base_sha, cwd=tmp_path) is ISABELLE


def test_base_sha_config_beats_default_for_rocq(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    base_sha = _commit_project_toml(tmp_path, "rocq", "base: rocq")
    assert resolve_prover_profile(None, base_sha, cwd=tmp_path) is ROCQ


def test_defaults_to_lean4_when_absent(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "README.md").write_text("hi\n", encoding="utf-8")
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "-q", "-m", "no project.toml")
    base_sha = _git(tmp_path, "rev-parse", "HEAD").strip()
    assert resolve_prover_profile(None, base_sha, cwd=tmp_path) is LEAN4


def test_flag_still_applies_even_with_no_project_toml_at_base(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    (tmp_path / "README.md").write_text("hi\n", encoding="utf-8")
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "-q", "-m", "no project.toml")
    base_sha = _git(tmp_path, "rev-parse", "HEAD").strip()
    assert resolve_prover_profile("rocq", base_sha, cwd=tmp_path) is ROCQ


def test_unknown_flag_raises_prover_error(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    base_sha = _commit_project_toml(tmp_path, "lean4", "base: lean4")
    with pytest.raises(ProverError, match="nope"):
        resolve_prover_profile("nope", base_sha, cwd=tmp_path)


def test_unknown_base_sha_value_raises_prover_error(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    base_sha = _commit_project_toml(tmp_path, "yolo", "base: bad value")
    with pytest.raises(ProverError, match="yolo"):
        resolve_prover_profile(None, base_sha, cwd=tmp_path)


def test_ignores_head_commits_prover_switch(tmp_path: Path) -> None:
    # The whole point of base-SHA reads: a later commit flipping the
    # prover must not change what a base-SHA resolution returns.
    _init_repo(tmp_path)
    base_sha = _commit_project_toml(tmp_path, "lean4", "base: lean4")
    _commit_project_toml(tmp_path, "rocq", "head: attempt to switch prover")
    assert resolve_prover_profile(None, base_sha, cwd=tmp_path) is LEAN4
