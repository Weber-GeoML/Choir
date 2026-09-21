"""Heredoc parity between `scripts/new-project.sh` and
`scripts/upgrade-project.sh`.

The two scripts duplicate the same GitHub Actions workflow templates as
bash heredocs (`upgrade-project.sh`'s header explains why: kept deliberately
unsourced from a shared file, "keep the two heredocs in sync by hand if
either changes"). Nothing enforces that by-hand promise, so the two files
have drifted before (a comment block present in one copy and missing in
the other, functional YAML unaffected). This test pins byte-equality for
the two workflow templates both scripts write for the SAME prover:

- the lean4 `verify-pr.yml` template (the first `verify-pr.yml` heredoc in
  each script, by textual order — both scripts write the lean4 variant
  before the isabelle/rocq ones).
- the `verify-trust-report.yml` template (unique per script; lean4-only).
"""

from __future__ import annotations

import difflib
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
NEW_PROJECT = REPO_ROOT / "scripts" / "new-project.sh"
UPGRADE_PROJECT = REPO_ROOT / "scripts" / "upgrade-project.sh"

_HEREDOC_RE_TEMPLATE = (
    r"cat > [^\n]*\.github/workflows/{filename}[^\n]*<<'YAML'\n(.*?)\nYAML\n"
)


def extract_heredoc(script_text: str, filename: str, occurrence: int = 0) -> str:
    """Return the body of the `occurrence`-th (0-indexed, textual order)
    ``cat > .../<filename> <<'YAML' ... YAML`` heredoc in `script_text`.

    Pure string function — no filesystem or subprocess access — so it's
    trivial to exercise directly against fixture strings as well as the
    real scripts.
    """
    pattern = re.compile(_HEREDOC_RE_TEMPLATE.format(filename=re.escape(filename)), re.DOTALL)
    matches = pattern.findall(script_text)
    if occurrence >= len(matches):
        raise AssertionError(
            f"expected at least {occurrence + 1} '{filename}' heredoc(s), found {len(matches)}"
        )
    return matches[occurrence]


def _assert_same(label: str, a: str, b: str) -> None:
    if a == b:
        return
    diff = "\n".join(
        difflib.unified_diff(
            a.splitlines(),
            b.splitlines(),
            fromfile=f"new-project.sh:{label}",
            tofile=f"upgrade-project.sh:{label}",
            lineterm="",
        )
    )
    raise AssertionError(
        f"{label} heredoc drifted between new-project.sh and upgrade-project.sh "
        f"(sync them by hand — see upgrade-project.sh's header note):\n{diff}"
    )


def test_extract_heredoc_pure_function() -> None:
    fixture = (
        "before\n"
        "cat > .github/workflows/foo.yml <<'YAML'\n"
        "line one\n"
        "line two\n"
        "YAML\n"
        "after\n"
    )
    assert extract_heredoc(fixture, "foo.yml") == "line one\nline two"


def test_lean4_verify_pr_heredoc_matches() -> None:
    new_project_text = NEW_PROJECT.read_text(encoding="utf-8")
    upgrade_project_text = UPGRADE_PROJECT.read_text(encoding="utf-8")

    # Both scripts write the lean4 verify-pr.yml template before any
    # isabelle/rocq variant, so the first occurrence in textual order is
    # always the lean4 one, regardless of where the two scripts otherwise
    # order their heredocs relative to verify-trust-report.yml.
    new_project_body = extract_heredoc(new_project_text, "verify-pr.yml", occurrence=0)
    upgrade_project_body = extract_heredoc(upgrade_project_text, "verify-pr.yml", occurrence=0)

    _assert_same("verify-pr.yml (lean4)", new_project_body, upgrade_project_body)


def test_verify_trust_report_heredoc_matches() -> None:
    new_project_text = NEW_PROJECT.read_text(encoding="utf-8")
    upgrade_project_text = UPGRADE_PROJECT.read_text(encoding="utf-8")

    new_project_body = extract_heredoc(new_project_text, "verify-trust-report.yml", occurrence=0)
    upgrade_project_body = extract_heredoc(
        upgrade_project_text, "verify-trust-report.yml", occurrence=0
    )

    _assert_same("verify-trust-report.yml", new_project_body, upgrade_project_body)
