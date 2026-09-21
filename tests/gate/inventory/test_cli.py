"""Tests for the inventory CLI."""

from __future__ import annotations

import json
from pathlib import Path

from gate.inventory.cli import main


def _project(tmp_path: Path) -> Path:
    (tmp_path / "P.lean").write_text(
        "axiom assumption : Nat\ntheorem t : True := by sorry\n", encoding="utf-8"
    )
    return tmp_path


def test_cli_scan_json(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    _project(tmp_path)
    code = main(["scan", str(tmp_path)])
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["summary"]["axiom_count"] == 1
    assert data["summary"]["sorry_count"] == 1
    assert data["sorries"][0]["decl"] == "t"


def test_cli_scan_md(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    _project(tmp_path)
    code = main(["scan", str(tmp_path), "--format", "md"])
    assert code == 0
    out = capsys.readouterr().out
    assert "# Trust boundary" in out
    assert "`assumption`" in out
    assert "`t`" in out


def test_cli_scan_bad_path(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    code = main(["scan", str(tmp_path / "missing")])
    assert code == 1
    assert "not a directory" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Prover resolution (whole-branch review finding A).
#
# `scan_tree` used to always run with the lean4 default profile, so a
# rocq/isabelle checkout's `.lean`-only file walk silently found nothing —
# the trust-boundary inventory reported empty on non-lean4 projects. The
# CLI now resolves the profile the same way `intake_cli`/`trust_report_cli`
# do: `--prover` flag, else `.choir/project.toml`'s `[project] prover`.
# ---------------------------------------------------------------------------


def _rocq_project(tmp_path: Path) -> Path:
    (tmp_path / ".choir").mkdir()
    (tmp_path / ".choir" / "project.toml").write_text(
        '[project]\nprover = "rocq"\n', encoding="utf-8"
    )
    (tmp_path / "Scratch.v").write_text(
        "Lemma add_comm : forall n m : nat, n + m = m + n.\n"
        "Proof.\n"
        "Admitted.\n",
        encoding="utf-8",
    )
    return tmp_path


def test_cli_scan_resolves_rocq_prover_from_project_toml(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    _rocq_project(tmp_path)
    code = main(["scan", str(tmp_path)])
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["summary"]["files_scanned"] == 1
    assert data["summary"]["sorry_count"] == 1
    assert data["sorries"][0]["decl"] == "add_comm"


def test_cli_scan_prover_flag_overrides_project_toml(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    _rocq_project(tmp_path)
    # Force lean4 despite the rocq project.toml: the .v file is ignored,
    # and no .lean files exist, so the scan finds nothing.
    code = main(["scan", str(tmp_path), "--prover", "lean4"])
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["summary"]["files_scanned"] == 0


def test_cli_scan_unknown_prover_flag_errors(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    code = main(["scan", str(tmp_path), "--prover", "bogus"])
    assert code == 1
    assert "unknown prover" in capsys.readouterr().err


def test_cli_scan_defaults_to_lean4_when_no_project_toml(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    # lean4 default is unchanged: a stray .v file is not picked up absent
    # an explicit [project] prover selection.
    _project(tmp_path)
    (tmp_path / "Extra.v").write_text("Admitted.\n", encoding="utf-8")
    code = main(["scan", str(tmp_path)])
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["summary"]["files_scanned"] == 1
