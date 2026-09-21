"""Guard: `new-project.sh --prover` wiring (design note 12 §7).

Mirrors `tests/client/test_join_recipe.py`'s precedent for smoke-checking
shell scripts that have no bash test harness: grep the script for the
load-bearing substrings rather than executing the whole bootstrap flow
(which needs a live `gh` session). A `bash -n` syntax check backs the
grep checks with an actual parse.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "new-project.sh"


def _text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_new_project_sh_has_valid_bash_syntax() -> None:
    result = subprocess.run(
        ["bash", "-n", str(SCRIPT)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_three_prover_cases_present() -> None:
    text = _text()
    # Flag validation names all three explicitly, usage included.
    assert "--prover" in text
    assert "lean4|isabelle|rocq) ;;" in text
    # Skeleton + workflow dispatch branch on lean4 and isabelle explicitly
    # (rocq is the trailing `else`, per the plan).
    assert '"$PROVER" == "lean4"' in text
    assert '"$PROVER" == "isabelle"' in text



def test_project_toml_writes_prover_selection() -> None:
    text = _text()
    assert "[project]" in text
    assert 'prover = "$PROVER"' in text


def test_project_toml_writes_protocol_pin() -> None:
    # design note 13 §3 — bootstrap writes the pin alongside prover, in
    # the same [project] heredoc, computed from the running Choir
    # checkout at bootstrap time.
    text = _text()
    assert "CHOIR_PROTOCOL_VERSION" in text
    assert "CHOIR_BOOTSTRAP_COMMIT" in text
    assert "choir_protocol = $CHOIR_PROTOCOL_VERSION" in text
    assert 'choir_commit = "$CHOIR_BOOTSTRAP_COMMIT"' in text
    assert "from gate.protocol import PROTOCOL_VERSION" in text


def test_lean4_ships_verify_trust_report_workflow() -> None:
    # lean4 projects get the autodetecting trust-report workflow
    # (design note 12 §4.1). isabelle/rocq ship it too now — see
    # test_all_provers_ship_verify_trust_report.
    text = _text()
    assert "verify-trust-report.yml" in text
    assert "name: verify-trust-report" in text
    assert "--base-sha ${{ github.event.pull_request.base.sha }}" in text
    # Adapted for the generated repo's own root as the workspace, not
    # samples/lean4 (that's the Choir repo's own layout).
    assert "--workspace ." in text


def test_rocq_skeleton_has_dune_coq_theory_stanza() -> None:
    # Without a `dune` file declaring the coq theory, `dune build` compiles
    # no .v files — so the generated rocq skeleton must ship one.
    text = _text()
    assert 'cat > "$PROJECT/$LIB/dune"' in text
    assert "(coq.theory" in text


def test_all_provers_ship_verify_trust_report() -> None:
    # Each of the three prover branches (lean4, isabelle, rocq) generates a
    # verify-trust-report workflow — one `name: verify-trust-report` apiece.
    assert _text().count("name: verify-trust-report") == 3
