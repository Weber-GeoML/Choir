"""Tests for `gate.provers.select` — prover selection readers.

Mirrors the tmp-git-repo pattern in `tests/gate/verify/test_axiom_honesty_cli.py`
for the base-SHA reader.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gate.provers import ProverError
from gate.provers.select import DEFAULT_PROVER, read_prover, read_prover_at_sha


def test_default_prover_is_lean4() -> None:
    assert DEFAULT_PROVER == "lean4"


def _write_project_toml(workspace: Path, project_body: str) -> None:
    (workspace / ".choir").mkdir(exist_ok=True)
    (workspace / ".choir" / "project.toml").write_text(
        f"[project]\n{project_body}", encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# read_prover — reads a workspace directory directly
# ---------------------------------------------------------------------------


def test_read_prover_absent_file_defaults_to_lean4(tmp_path: Path) -> None:
    assert read_prover(tmp_path) == "lean4"


def test_read_prover_explicit_lean4(tmp_path: Path) -> None:
    _write_project_toml(tmp_path, 'prover = "lean4"\n')
    assert read_prover(tmp_path) == "lean4"


def test_read_prover_absent_project_section_defaults_to_lean4(tmp_path: Path) -> None:
    (tmp_path / ".choir").mkdir()
    (tmp_path / ".choir" / "project.toml").write_text(
        '[automation]\nmerge = "auto"\n', encoding="utf-8"
    )
    assert read_prover(tmp_path) == "lean4"


def test_read_prover_unknown_value_raises(tmp_path: Path) -> None:
    _write_project_toml(tmp_path, 'prover = "coq_legacy"\n')
    with pytest.raises(ProverError, match="coq_legacy"):
        read_prover(tmp_path)


def test_read_prover_malformed_toml_raises(tmp_path: Path) -> None:
    (tmp_path / ".choir").mkdir()
    (tmp_path / ".choir" / "project.toml").write_text(
        "this = is = not = toml\n", encoding="utf-8"
    )
    with pytest.raises(ProverError, match="malformed"):
        read_prover(tmp_path)


# ---------------------------------------------------------------------------
# read_prover_at_sha — reads via `git show <sha>:.choir/project.toml`
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return result.stdout


def _init_repo(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")


def test_read_prover_at_sha_missing_file_defaults_to_lean4(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "README.md").write_text("hi\n", encoding="utf-8")
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "-q", "-m", "no project.toml")
    sha = _git(tmp_path, "rev-parse", "HEAD").strip()
    assert read_prover_at_sha(sha, cwd=tmp_path) == "lean4"


def test_read_prover_at_sha_reads_committed_value(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_project_toml(tmp_path, 'prover = "lean4"\n')
    _git(tmp_path, "add", ".choir/project.toml")
    _git(tmp_path, "commit", "-q", "-m", "pin lean4")
    sha = _git(tmp_path, "rev-parse", "HEAD").strip()
    assert read_prover_at_sha(sha, cwd=tmp_path) == "lean4"


def test_read_prover_at_sha_unknown_value_raises(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_project_toml(tmp_path, 'prover = "yolo"\n')
    _git(tmp_path, "add", ".choir/project.toml")
    _git(tmp_path, "commit", "-q", "-m", "bad prover")
    sha = _git(tmp_path, "rev-parse", "HEAD").strip()
    with pytest.raises(ProverError, match="yolo"):
        read_prover_at_sha(sha, cwd=tmp_path)


def test_read_prover_at_sha_ignores_a_later_commits_change(tmp_path: Path) -> None:
    # Mirrors the base-SHA discipline pinned for the other gate config
    # readers (gate/verify/config.py::load_verify_config_at_sha): a PR
    # that flips `[project] prover` at head must not change what a
    # base-SHA read resolves.
    _init_repo(tmp_path)
    _write_project_toml(tmp_path, 'prover = "lean4"\n')
    _git(tmp_path, "add", ".choir/project.toml")
    _git(tmp_path, "commit", "-q", "-m", "base: lean4")
    base_sha = _git(tmp_path, "rev-parse", "HEAD").strip()

    _write_project_toml(tmp_path, 'prover = "bogus"\n')
    _git(tmp_path, "add", ".choir/project.toml")
    _git(tmp_path, "commit", "-q", "-m", "head: attempt to switch prover")

    assert read_prover_at_sha(base_sha, cwd=tmp_path) == "lean4"
