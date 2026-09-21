"""Tests for `gate.verify.trust_report_cli` (design note 12 §4/§4.1).

Informational check: exit 0 on any successful probe run (clean or
not), exit 2 only for infrastructure errors (bad workspace, unknown
prover, probe subprocess failure, git-plumbing failure in
autodetection). `collect_trust_report` and `detect_changed_decls` are
monkeypatched at the CLI's import site so these tests don't need a
real Lean/Rocq/Isabelle toolchain or a real git repo — the live probe
is exercised in tests/gate/provers/test_trust.py and the real
git-fixture detection logic in tests/gate/verify/test_changed_decls.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gate.provers import ProverError
from gate.provers.base import TrustEntry
from gate.provers.lean4 import LEAN4
from gate.verify.changed_decls import DEFAULT_CAP, ChangedDecl, ChangedDeclsError
from gate.verify.trust_report_cli import _run_auto_mode, main


def write_project_toml(workspace: Path, prover: str | None) -> Path:
    d = workspace / ".choir"
    d.mkdir(parents=True, exist_ok=True)
    if prover is not None:
        (d / "project.toml").write_text(f'[project]\nprover = "{prover}"\n')
    return workspace


def test_nonexistent_workspace_is_infrastructure_error(tmp_path, capsys):
    rc = main(["--workspace", str(tmp_path / "nope")])
    assert rc == 2
    assert "not a directory" in capsys.readouterr().err


def test_no_decls_is_a_documented_noop(tmp_path, capsys):
    rc = main(["--workspace", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "informational check, nothing to probe" in out


def test_unknown_prover_flag_is_infrastructure_error(tmp_path, capsys):
    rc = main(["--workspace", str(tmp_path), "--prover", "nope", "--decl", "foo"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "nope" in err


def test_prover_defaults_to_lean4_when_absent(tmp_path, capsys):
    rc = main(["--workspace", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "prover:    lean4" in out


def test_prover_read_from_workspace_project_toml(tmp_path, capsys):
    write_project_toml(tmp_path, "rocq")
    rc = main(["--workspace", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "prover:    rocq" in out


def test_prover_flag_overrides_workspace_project_toml(tmp_path, capsys):
    write_project_toml(tmp_path, "rocq")
    rc = main(["--workspace", str(tmp_path), "--prover", "isabelle"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "prover:    isabelle" in out


def test_successful_probe_reports_clean_and_dirty_entries(tmp_path, capsys, monkeypatch):
    def fake_collect(profile, workspace, decls, imports):
        return [
            TrustEntry(decl="t", assumptions=(), clean=True),
            TrustEntry(decl="uses_bad", assumptions=("bad",), clean=False),
        ]

    monkeypatch.setattr("gate.verify.trust_report_cli.collect_trust_report", fake_collect)
    rc = main(["--workspace", str(tmp_path), "--decl", "t", "--decl", "uses_bad"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "✓ t: closed (no axioms/oracles)" in out
    assert "⚠ uses_bad: depends on bad" in out
    assert "Informational only" in out


def test_probe_reports_missing_decls_not_covered_by_the_output(
    tmp_path, capsys, monkeypatch
):
    def fake_collect(profile, workspace, decls, imports):
        return [TrustEntry(decl="t", assumptions=(), clean=True)]

    monkeypatch.setattr("gate.verify.trust_report_cli.collect_trust_report", fake_collect)
    rc = main(["--workspace", str(tmp_path), "--decl", "t", "--decl", "missing_decl"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "no trust-report line for: missing_decl" in out


def test_prover_error_from_collect_is_infrastructure_error(tmp_path, capsys, monkeypatch):
    def fake_collect(profile, workspace, decls, imports):
        raise ProverError("trust-report probe failed (exit 1): boom")

    monkeypatch.setattr("gate.verify.trust_report_cli.collect_trust_report", fake_collect)
    rc = main(["--workspace", str(tmp_path), "--decl", "t"])
    err = capsys.readouterr().err

    assert rc == 2
    assert "boom" in err


def test_imports_flag_is_passed_through(tmp_path, monkeypatch):
    seen = {}

    def fake_collect(profile, workspace, decls, imports):
        seen["decls"] = decls
        seen["imports"] = imports
        return []

    monkeypatch.setattr("gate.verify.trust_report_cli.collect_trust_report", fake_collect)
    rc = main(
        [
            "--workspace",
            str(tmp_path),
            "--decl",
            "foo",
            "--decl",
            "bar",
            "--import",
            "Mod1",
            "--import",
            "Mod2",
        ]
    )
    assert rc == 0
    assert seen["decls"] == ["foo", "bar"]
    assert seen["imports"] == ["Mod1", "Mod2"]


@pytest.mark.parametrize("argv", [[], ["--workspace"]])
def test_missing_required_workspace_arg_errors(argv):
    with pytest.raises(SystemExit):
        main(argv)


# ---------------------------------------------------------------------------
# Auto mode (--base-sha), design note 12 §4.1.
# ---------------------------------------------------------------------------


def test_base_sha_and_decl_are_mutually_exclusive(tmp_path, capsys):
    rc = main(
        ["--workspace", str(tmp_path), "--decl", "foo", "--base-sha", "deadbeef"]
    )
    err = capsys.readouterr().err
    assert rc == 2
    assert "mutually exclusive" in err


def test_auto_mode_empty_detection_is_a_documented_noop(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(
        "gate.verify.trust_report_cli.detect_changed_decls",
        lambda workspace, base_sha, profile, cap=DEFAULT_CAP: ([], False),
    )
    rc = main(["--workspace", str(tmp_path), "--base-sha", "abc123"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "No changed declarations detected" in out


def test_auto_mode_mixed_clean_dirty_and_unresolved(tmp_path, capsys, monkeypatch):
    targets = [
        ChangedDecl(name="clean_one", file="F.lean", module="F"),
        ChangedDecl(name="dirty_one", file="F.lean", module="F"),
        ChangedDecl(name="broken_one", file="F.lean", module="F"),
    ]
    monkeypatch.setattr(
        "gate.verify.trust_report_cli.detect_changed_decls",
        lambda workspace, base_sha, profile, cap=DEFAULT_CAP: (targets, False),
    )

    def fake_collect(profile, workspace, decls, imports):
        assert len(decls) == 1
        name = decls[0]
        if name == "clean_one":
            return [TrustEntry(decl="clean_one", assumptions=(), clean=True)]
        if name == "dirty_one":
            return [TrustEntry(decl="dirty_one", assumptions=("bad",), clean=False)]
        raise ProverError("trust-report probe failed (exit 1): unknown constant\nmore detail")

    monkeypatch.setattr("gate.verify.trust_report_cli.collect_trust_report", fake_collect)

    rc = main(["--workspace", str(tmp_path), "--base-sha", "abc123"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "✓ clean_one: closed (no axioms/oracles)" in out
    assert "⚠ dirty_one: depends on bad" in out
    assert "unresolved" in out
    assert "broken_one: trust-report probe failed (exit 1): unknown constant" in out
    # Round 5 (F3): the whole transcript is kept, continuation lines
    # indented. A first-line summary reported isabelle's `Loading theory
    # "…"` notice as the reason a declaration was unresolved, because
    # `ML_process` prints that (and a `###` timing line) *before* the
    # exception — while lean4 puts its compile error first. There is no
    # prover-generic rule for which line matters, so none is guessed.
    assert "      more detail" in out
    assert "Informational only" in out


def test_auto_mode_reports_cap_truncation(tmp_path, capsys, monkeypatch):
    targets = [ChangedDecl(name="t", file="F.lean", module="F")]
    monkeypatch.setattr(
        "gate.verify.trust_report_cli.detect_changed_decls",
        lambda workspace, base_sha, profile, cap=DEFAULT_CAP: (targets, True),
    )
    monkeypatch.setattr(
        "gate.verify.trust_report_cli.collect_trust_report",
        lambda profile, workspace, decls, imports: [
            TrustEntry(decl="t", assumptions=(), clean=True)
        ],
    )
    rc = main(["--workspace", str(tmp_path), "--base-sha", "abc123"])
    out = capsys.readouterr().out
    assert rc == 0
    assert f"capped at {DEFAULT_CAP}" in out


def test_auto_mode_cap_message_reflects_actual_cap_not_the_default_constant(
    tmp_path, capsys, monkeypatch
):
    # Regression guard for the "cap message honesty" fix: the truncation
    # note must derive from the cap value actually passed to
    # detect_changed_decls, not a hardcoded DEFAULT_CAP reference that
    # could silently drift from it. Drives `_run_auto_mode` directly with a
    # non-default cap (7) since `main()` has no `--cap` flag to exercise
    # this through the argparse front-end.
    targets = [ChangedDecl(name="t", file="F.lean", module="F")]
    seen_cap = {}

    def fake_detect(workspace, base_sha, profile, cap=DEFAULT_CAP):
        seen_cap["cap"] = cap
        return targets, True

    monkeypatch.setattr(
        "gate.verify.trust_report_cli.detect_changed_decls", fake_detect
    )
    monkeypatch.setattr(
        "gate.verify.trust_report_cli.collect_trust_report",
        lambda profile, workspace, decls, imports: [
            TrustEntry(decl="t", assumptions=(), clean=True)
        ],
    )
    rc = _run_auto_mode(LEAN4, tmp_path, "abc123", cap=7)
    out = capsys.readouterr().out
    assert rc == 0
    assert seen_cap["cap"] == 7
    assert "capped at 7" in out
    assert "capped at 50" not in out


def test_auto_mode_git_failure_is_infrastructure_error(tmp_path, capsys, monkeypatch):
    def fake_detect(workspace, base_sha, profile, cap=DEFAULT_CAP):
        raise ChangedDeclsError("git diff --name-only abc123 failed: bad revision")

    monkeypatch.setattr(
        "gate.verify.trust_report_cli.detect_changed_decls", fake_detect
    )
    rc = main(["--workspace", str(tmp_path), "--base-sha", "abc123"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "bad revision" in err


def test_auto_mode_passes_decl_name_and_module_as_import(tmp_path, monkeypatch):
    targets = [ChangedDecl(name="Foo.bar", file="a/Foo.lean", module="a.Foo")]
    monkeypatch.setattr(
        "gate.verify.trust_report_cli.detect_changed_decls",
        lambda workspace, base_sha, profile, cap=DEFAULT_CAP: (targets, False),
    )
    seen = {}

    def fake_collect(profile, workspace, decls, imports):
        seen["decls"] = decls
        seen["imports"] = imports
        return [TrustEntry(decl="Foo.bar", assumptions=(), clean=True)]

    monkeypatch.setattr("gate.verify.trust_report_cli.collect_trust_report", fake_collect)
    rc = main(["--workspace", str(tmp_path), "--base-sha", "abc123"])
    assert rc == 0
    assert seen["decls"] == ["Foo.bar"]
    assert seen["imports"] == ["a.Foo"]
