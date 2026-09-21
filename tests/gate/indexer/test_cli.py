"""Tests for the coherence indexer CLI (`gate/indexer/cli.py`).

Whole-branch review finding B: `scan_directory(base_root)`/`scan_directory
(head_root)` used to always run with the lean4 default profile, so a
non-lean4 checkout's declarations were silently invisible (the lean4
profile only walks `.lean` files). The CLI now resolves the profile from
`--base-dir` — the PR's base-SHA checkout, same discipline as the
PR-triggered verify audits — with a `--prover` override flag mirroring
`gate/inventory/cli.py` and `gate/state/intake_cli.py`.
"""

from __future__ import annotations

from pathlib import Path

from gate.indexer.cli import main


def _lean4_pair(tmp_path: Path) -> tuple[Path, Path]:
    base = tmp_path / "base"
    head = tmp_path / "head"
    base.mkdir()
    head.mkdir()
    (base / "A.lean").write_text("theorem existing : True := trivial\n", encoding="utf-8")
    (head / "A.lean").write_text(
        "theorem existing : True := trivial\n"
        "theorem brand_new : True := trivial\n",
        encoding="utf-8",
    )
    return base, head


def _isabelle_pair(tmp_path: Path) -> tuple[Path, Path]:
    base = tmp_path / "base"
    head = tmp_path / "head"
    base.mkdir()
    head.mkdir()
    (base / ".choir").mkdir()
    (base / ".choir" / "project.toml").write_text(
        '[project]\nprover = "isabelle"\n', encoding="utf-8"
    )
    (base / "Scratch.thy").write_text(
        "theory Scratch imports Main begin\nend\n", encoding="utf-8"
    )
    (head / "Scratch.thy").write_text(
        "theory Scratch imports Main begin\n\n"
        'lemma add_comm_nat: "a + b = b + (a::nat)"\n'
        "  by simp\n\n"
        "end\n",
        encoding="utf-8",
    )
    return base, head


def test_cli_resolves_isabelle_prover_from_base_dir(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    base, head = _isabelle_pair(tmp_path)
    code = main(["--base-dir", str(base), "--head-dir", str(head)])
    assert code == 0
    out = capsys.readouterr().out
    assert "declarations in base: 0" in out
    assert "declarations in head: 1" in out
    assert "new declarations in head: 1" in out


def test_cli_prover_flag_overrides_project_toml(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    base, head = _isabelle_pair(tmp_path)
    # Force lean4 despite the isabelle project.toml: the .thy files are
    # ignored (lean4 only walks .lean), so nothing is found either side.
    code = main(["--base-dir", str(base), "--head-dir", str(head), "--prover", "lean4"])
    assert code == 0
    out = capsys.readouterr().out
    assert "declarations in base: 0" in out
    assert "declarations in head: 0" in out


def test_cli_unknown_prover_flag_errors(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    base, head = _lean4_pair(tmp_path)
    code = main(["--base-dir", str(base), "--head-dir", str(head), "--prover", "bogus"])
    assert code == 1
    assert "unknown prover" in capsys.readouterr().err


def test_cli_defaults_to_lean4_when_no_project_toml(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    base, head = _lean4_pair(tmp_path)
    code = main(["--base-dir", str(base), "--head-dir", str(head)])
    assert code == 0
    out = capsys.readouterr().out
    assert "declarations in base: 1" in out
    assert "declarations in head: 2" in out
    assert "new declarations in head: 1" in out
