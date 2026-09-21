"""Tests for `gate.verify.axiom_honesty_cli.load_verify_config_from_base`.

The full CLI dispatch (gh + git against a remote PR) is exercised in
the demo; these tests pin down the security-critical part: the audit
policy must come from the *base* SHA, not the PR head — otherwise a
contributor could weaken the whitelist in their own PR.

Also pins down (task 6, 2026-08-20) that an unresolvable `--prover`
exits 2 rather than escaping `main()` as an uncaught `ProverError` —
the same fix `statement_immutability_cli` already had, now applied
uniformly across every CLI that resolves a prover from the base SHA.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gate.provers import ProverError
from gate.provers.isabelle import ISABELLE
from gate.provers.lean4 import LEAN4
from gate.provers.rocq import ROCQ
from gate.verify import axiom_honesty_cli as cli
from gate.verify.axiom_honesty_cli import (
    filter_files_by_profile,
    load_verify_config_from_base,
)
from gate.verify.config import AxiomPolicy, VerifyConfig


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return result.stdout


def _init_repo_with_policy_history(tmp_path: Path) -> tuple[str, str]:
    """Set up a tmp git repo:

    - base SHA: `.choir/verify.toml` declares `policy = "whitelist"`,
      `allowed_axioms = ["propext"]`.
    - head SHA: same file but loosened to allow `bad_axiom` as well
      (simulates a malicious PR trying to whitelist its own bad axiom).
    """
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / ".choir").mkdir()
    (tmp_path / ".choir" / "verify.toml").write_text(
        '[audits.axiom_honesty]\n'
        'policy = "whitelist"\n'
        'allowed_axioms = ["propext"]\n',
        encoding="utf-8",
    )
    _git(tmp_path, "add", ".choir/verify.toml")
    _git(tmp_path, "commit", "-q", "-m", "base: maintainer policy")
    base_sha = _git(tmp_path, "rev-parse", "HEAD").strip()

    # Head: contributor tries to add their own bad axiom to the
    # whitelist via the PR.
    (tmp_path / ".choir" / "verify.toml").write_text(
        '[audits.axiom_honesty]\n'
        'policy = "whitelist"\n'
        'allowed_axioms = ["propext", "bad_axiom"]\n',
        encoding="utf-8",
    )
    _git(tmp_path, "add", ".choir/verify.toml")
    _git(tmp_path, "commit", "-q", "-m", "head: attempt to weaken policy")
    head_sha = _git(tmp_path, "rev-parse", "HEAD").strip()

    return base_sha, head_sha


def test_loads_whitelist_policy_from_base(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    base_sha, _head = _init_repo_with_policy_history(tmp_path)
    monkeypatch.chdir(tmp_path)
    cfg = load_verify_config_from_base(base_sha)
    assert cfg.axiom_honesty.policy is AxiomPolicy.WHITELIST
    assert cfg.axiom_honesty.allowed_axioms == ("propext",)


def test_ignores_head_policy_weakening(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # The whole point of base-SHA reads: a PR that weakens the policy
    # does NOT change what the audit enforces. We pass base_sha, so
    # the contributor's `allowed_axioms = ["propext", "bad_axiom"]`
    # in head is irrelevant.
    base_sha, _head = _init_repo_with_policy_history(tmp_path)
    monkeypatch.chdir(tmp_path)
    cfg = load_verify_config_from_base(base_sha)
    assert "bad_axiom" not in cfg.axiom_honesty.allowed_axioms


def test_missing_config_at_base_yields_defaults(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "README.md").write_text("hi\n", encoding="utf-8")
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "-q", "-m", "no config file")
    base_sha = _git(tmp_path, "rev-parse", "HEAD").strip()
    monkeypatch.chdir(tmp_path)
    assert load_verify_config_from_base(base_sha) == VerifyConfig()


def test_malformed_config_at_base_falls_back_to_defaults(
    tmp_path: Path, monkeypatch, capsys  # type: ignore[no-untyped-def]
) -> None:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / ".choir").mkdir()
    (tmp_path / ".choir" / "verify.toml").write_text(
        '[audits.axiom_honesty]\npolicy = "bogus_value"\n', encoding="utf-8"
    )
    _git(tmp_path, "add", ".choir/verify.toml")
    _git(tmp_path, "commit", "-q", "-m", "base with bad config")
    base_sha = _git(tmp_path, "rev-parse", "HEAD").strip()
    monkeypatch.chdir(tmp_path)
    cfg = load_verify_config_from_base(base_sha)
    assert cfg == VerifyConfig()  # safe default
    captured = capsys.readouterr()
    assert "warning" in captured.err.lower()


# ---------------------------------------------------------------------------
# filter_files_by_profile — per-prover extension filtering
# ---------------------------------------------------------------------------


def test_filter_files_lean4_keeps_only_lean() -> None:
    files = ["A.lean", "README.md", "sub/B.lean"]
    assert filter_files_by_profile(files, LEAN4) == ["A.lean", "sub/B.lean"]


def test_filter_files_isabelle_keeps_only_thy() -> None:
    files = ["Scratch.thy", "A.lean", "ROOT"]
    assert filter_files_by_profile(files, ISABELLE) == ["Scratch.thy"]


def test_filter_files_rocq_keeps_only_v() -> None:
    files = ["Scratch.v", "A.lean", "_CoqProject"]
    assert filter_files_by_profile(files, ROCQ) == ["Scratch.v"]


# ---------------------------------------------------------------------------
# An unresolvable prover exits 2, not a Python traceback (task 6, 2026-08-20)
# ---------------------------------------------------------------------------


def test_unresolvable_prover_exits_two(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unknown `--prover` (or, by the same code path, a malformed
    `[project] prover` in the base-SHA `.choir/project.toml`) must not
    escape `main()` as an uncaught `ProverError` — that would print a
    raw traceback and exit with Python's default 1, which the exit
    contract reserves for "the contributor introduced an axiom.\""""
    monkeypatch.setattr(
        cli, "fetch_pr_files", lambda repo, pr: ("base", "head", ["A.lean"])
    )

    def _raise(flag: str | None, base_sha: str) -> object:
        raise ProverError("unknown prover 'bogus'; valid: isabelle, lean4, rocq")

    monkeypatch.setattr(cli, "resolve_prover_profile", _raise)

    code = cli.main(["--repo", "o/r", "--pr", "7", "--prover", "bogus"])
    err = capsys.readouterr().err
    assert code == 2
    assert "unknown prover 'bogus'" in err
    assert "isabelle, lean4, rocq" in err
