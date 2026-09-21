"""Tests for `gate.verify.style_cli.filter_files_by_profile`.

The full CLI dispatch (gh/git against a remote PR) is exercised in the
demo; this pins down the per-prover extension filtering added by the
prover-profile generalization (design note 12 §2.3).
"""

from __future__ import annotations

from gate.provers.isabelle import ISABELLE
from gate.provers.lean4 import LEAN4
from gate.provers.rocq import ROCQ
from gate.verify import style_cli
from gate.verify.style_cli import filter_files_by_profile


def test_filter_files_lean4_keeps_only_lean() -> None:
    files = ["A.lean", "README.md", "sub/B.lean"]
    assert filter_files_by_profile(files, LEAN4) == ["A.lean", "sub/B.lean"]


def test_filter_files_isabelle_keeps_only_thy() -> None:
    files = ["Scratch.thy", "A.lean", "ROOT"]
    assert filter_files_by_profile(files, ISABELLE) == ["Scratch.thy"]


def test_filter_files_rocq_keeps_only_v() -> None:
    files = ["Scratch.v", "A.lean", "_CoqProject"]
    assert filter_files_by_profile(files, ROCQ) == ["Scratch.v"]


def test_over_threshold_reports_but_exits_zero(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    """D9: style is advisory. It must still name the offenders."""
    monkeypatch.setattr(
        style_cli, "fetch_pr_files", lambda repo, pr: ("base", "head", ["A.lean"])
    )
    monkeypatch.setattr(style_cli, "_show", lambda sha, path: "")
    monkeypatch.setattr(
        style_cli,
        "compare",
        lambda base, head, *, threshold, profile: (style_cli.Verdict.INTRODUCED, []),
    )
    monkeypatch.setattr(
        style_cli, "format_findings", lambda findings: "  Project.big_lemma: 300 lines"
    )

    code = style_cli.main(["--repo", "o/r", "--pr", "7", "--threshold-lines", "10"])
    out = capsys.readouterr().out
    assert code == 0
    assert "Project.big_lemma" in out
    assert "advisory" in out.lower()
