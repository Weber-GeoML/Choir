"""Tests for `gate.verify.statement_immutability_cli`.

The full CLI dispatch against a real PR (gh + git over the network) is
exercised in the demo. These tests pin down:

- per-prover extension filtering (mirrors every other delta-audit CLI);
- `_show` against a real tmp git repo, including the "changed file has
  no base version" case (a newly added file) — it must return '' and
  never crash on `git show`ing a path absent at that SHA;
- the exit-code mapping via a monkeypatched `fetch_pr_files`/`_show`/
  `compare_declarations` (mirrors `test_style_cli.py`'s pattern for
  exercising `main()` without touching gh/git): `UNCHANGED` -> 0,
  `CHANGED` -> 1, `UNDETERMINED` -> 1 (the semantic that differs from
  `statement_equiv`, where undetermined passes), no matching files ->
  0, and an external (gh/git) failure -> 2.
- `format_findings` keeps `None` (absent from head) and `""` (present
  but unparseable) visually distinct, since the orchestrator triages
  the two differently.
- review findings F1 (a same-PR rename must not bypass the audit —
  `_base_show` follows the rename via `detect_rename`), F2 (a real
  `git show` failure on a path that DOES exist at that SHA must raise
  rather than collapse into the legitimate new-file case), and F3 (an
  unresolvable `--prover` exits 2, not a Python traceback).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gate.provers import ProverError
from gate.provers.isabelle import ISABELLE
from gate.provers.lean4 import LEAN4
from gate.provers.rocq import ROCQ
from gate.verify import statement_immutability_cli as cli
from gate.verify.statement_immutability import Finding, Verdict
from gate.verify.statement_immutability_cli import (
    _ExternalError,
    _show,
    filter_files_by_profile,
    format_findings,
)


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return result.stdout


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
# _show — real git, including the no-base-version (newly added file) case
# ---------------------------------------------------------------------------


def _init_repo_with_add_then_modify(tmp_path: Path) -> tuple[str, str]:
    """Base SHA has `Existing.lean` only; head SHA adds `New.lean` too."""
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "Existing.lean").write_text(
        "theorem foo : 1 = 1 := rfl\n", encoding="utf-8"
    )
    _git(tmp_path, "add", "Existing.lean")
    _git(tmp_path, "commit", "-q", "-m", "base")
    base_sha = _git(tmp_path, "rev-parse", "HEAD").strip()

    (tmp_path / "New.lean").write_text(
        "theorem bar : 2 = 2 := rfl\n", encoding="utf-8"
    )
    _git(tmp_path, "add", "New.lean")
    _git(tmp_path, "commit", "-q", "-m", "head: add a new file")
    head_sha = _git(tmp_path, "rev-parse", "HEAD").strip()
    return base_sha, head_sha


def test_show_returns_empty_string_for_path_absent_at_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base_sha, head_sha = _init_repo_with_add_then_modify(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert _show(base_sha, "New.lean") == ""
    assert _show(head_sha, "New.lean") == "theorem bar : 2 = 2 := rfl\n"


def test_show_returns_existing_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base_sha, _head_sha = _init_repo_with_add_then_modify(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert _show(base_sha, "Existing.lean") == "theorem foo : 1 = 1 := rfl\n"


def test_show_returns_empty_for_unresolvable_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unresolvable SHA reads as "path absent there," not a distinct
    external-failure case: `git cat-file -e` fails for the same reason
    `git show` would (there is no tree to look the path up in), so this
    correctly falls into the legitimate-new-file branch, not F2's
    exists-but-unreadable branch (see
    `test_show_raises_when_path_exists_but_git_show_fails` below for
    that one, which this test used to conflate with this case before
    the fix)."""
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "A.lean").write_text("theorem foo : 1 = 1 := rfl\n", encoding="utf-8")
    _git(tmp_path, "add", "A.lean")
    _git(tmp_path, "commit", "-q", "-m", "base")
    monkeypatch.chdir(tmp_path)
    assert _show("not-a-real-sha", "A.lean") == ""


def test_show_raises_when_path_exists_but_git_show_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F2: existence-true but `git show`-fails must raise, not collapse
    into the same '' a legitimately new file gets.

    Forces `_exists_at` to report True for a path that genuinely has no
    blob at `base_sha`, simulating the case F2 is about — the existence
    check said "yes" (a real base file, or the check itself is
    confused), but reading it still failed. That must surface as an
    external failure, not read as "nothing to check here."
    """
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "A.lean").write_text("theorem foo : 1 = 1 := rfl\n", encoding="utf-8")
    _git(tmp_path, "add", "A.lean")
    _git(tmp_path, "commit", "-q", "-m", "base")
    base_sha = _git(tmp_path, "rev-parse", "HEAD").strip()
    monkeypatch.chdir(tmp_path)

    monkeypatch.setattr(cli, "_exists_at", lambda sha, path: True)

    with pytest.raises(_ExternalError):
        cli._show(base_sha, "DoesNotReallyExist.lean")


def test_new_file_in_pr_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """F2: a genuinely new file (absent at base) is a clean pass through
    the full pipeline, not an error and not a false failure.

    Round 4 (F3) additionally requires it not to *read* as a pass: a new
    file has no base version, so there was nothing to check, and the
    report has to say which of the two it is rather than printing the
    same "clean" line a genuinely-verified file gets."""
    base_sha, head_sha = _init_repo_with_add_then_modify(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        cli, "fetch_pr_files", lambda repo, pr: (base_sha, head_sha, ["New.lean"])
    )

    code = cli.main(["--repo", "o/r", "--pr", "7"])
    out = capsys.readouterr().out
    assert code == 0
    assert "New.lean: no declarations in the base version; nothing to check" in out
    assert "clean" not in out


def test_show_failure_mid_loop_exits_two(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """F2 at the `main()` level: a real content-fetch failure on a
    changed file exits 2, distinct from both a clean pass and an audit
    failure."""
    monkeypatch.setattr(
        cli, "fetch_pr_files", lambda repo, pr: ("base", "head", ["A.lean"])
    )

    def _raise(base_sha: str, head_sha: str, path: str) -> str:
        raise _ExternalError("git show base:A.lean failed: fatal: unable to read blob")

    monkeypatch.setattr(cli, "_base_show", _raise)

    code = cli.main(["--repo", "o/r", "--pr", "7"])
    err = capsys.readouterr().err
    assert code == 2
    assert "unable to read blob" in err


# ---------------------------------------------------------------------------
# F1 — a same-PR rename must not bypass the audit
# ---------------------------------------------------------------------------


_BASE_FILE_TEXT = (
    "-- Some header comment about this file.\n"
    "-- More context, so the file has enough content for git's\n"
    "-- similarity heuristic to recognize the rename below.\n"
    "theorem foo : 1 = 1 := rfl\n"
)
_HEAD_FILE_TEXT = (
    "-- Some header comment about this file.\n"
    "-- More context, so the file has enough content for git's\n"
    "-- similarity heuristic to recognize the rename below.\n"
    "theorem foo : 1 = 2 := rfl\n"
)


def _init_repo_with_rename_and_statement_change(tmp_path: Path) -> tuple[str, str]:
    """Base SHA has `Foo.lean` with `theorem foo`; head SHA renames it to
    `Bar.lean` AND changes `foo`'s statement — the F1 exploit shape.

    The file carries a few extra comment lines so git's rename
    similarity heuristic actually fires: a bare single-line file where
    the whole line changes does not score high enough (verified
    directly — a 1-line `theorem foo : 1 = 1 := rfl` -> `theorem foo :
    1 = 2 := rfl` rename reads as a plain add+delete, not `Rxxx`, at
    any `-M` threshold down to 10%), which would make this fixture
    fail to exercise the rename-following path it exists to test.
    """
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "Foo.lean").write_text(_BASE_FILE_TEXT, encoding="utf-8")
    _git(tmp_path, "add", "Foo.lean")
    _git(tmp_path, "commit", "-q", "-m", "base")
    base_sha = _git(tmp_path, "rev-parse", "HEAD").strip()

    _git(tmp_path, "mv", "Foo.lean", "Bar.lean")
    (tmp_path / "Bar.lean").write_text(_HEAD_FILE_TEXT, encoding="utf-8")
    _git(tmp_path, "add", "Bar.lean")
    _git(tmp_path, "commit", "-q", "-m", "rename + change statement")
    head_sha = _git(tmp_path, "rev-parse", "HEAD").strip()
    return base_sha, head_sha


def test_base_show_follows_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base_sha, head_sha = _init_repo_with_rename_and_statement_change(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli._base_show(base_sha, head_sha, "Bar.lean") == _BASE_FILE_TEXT


def test_rename_with_statement_change_is_caught(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """F1: renaming a file while rewriting its declaration must not pass
    clean. Before the fix, `Bar.lean` had no base version under that
    name, so this exact PR shape reported "clean" and exited 0."""
    base_sha, head_sha = _init_repo_with_rename_and_statement_change(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        cli, "fetch_pr_files", lambda repo, pr: (base_sha, head_sha, ["Bar.lean"])
    )

    code = cli.main(["--repo", "o/r", "--pr", "7"])
    out = capsys.readouterr().out
    assert code == 1
    assert "foo" in out


# ---------------------------------------------------------------------------
# main() exit-code mapping — mocked fetch_pr_files/_show/compare_declarations
# (mirrors test_style_cli.py's pattern for exercising the dispatch without
# touching gh/git over the network)
# ---------------------------------------------------------------------------


def test_unchanged_verdict_exits_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        cli, "fetch_pr_files", lambda repo, pr: ("base", "head", ["A.lean"])
    )
    monkeypatch.setattr(
        cli, "_show", lambda sha, path: "theorem foo : 1 = 1 := rfl\n"
    )
    monkeypatch.setattr(
        cli,
        "compare_declarations",
        lambda base, head, *, profile: (Verdict.UNCHANGED, []),
    )

    code = cli.main(["--repo", "o/r", "--pr", "7"])
    out = capsys.readouterr().out
    assert code == 0
    # Round 4 (F3): a real UNCHANGED says how many base declarations it
    # actually compared, so it cannot be confused with the
    # nothing-to-check line the zero-declaration case prints.
    assert "A.lean: clean (1 base declaration checked)" in out


def test_unchanged_verdict_with_no_base_declarations_says_nothing_to_check(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Round 4, F3: the same UNCHANGED verdict over a base version with no
    enumerable declarations must not render as "checked and fine". Both
    are UNCHANGED — correctly, nothing moved either way — but only one of
    them verified anything, and the orchestrator reads these lines to
    decide where to look."""
    monkeypatch.setattr(
        cli, "fetch_pr_files", lambda repo, pr: ("base", "head", ["A.lean"])
    )
    monkeypatch.setattr(cli, "_show", lambda sha, path: "-- only a comment\n")
    monkeypatch.setattr(
        cli,
        "compare_declarations",
        lambda base, head, *, profile: (Verdict.UNCHANGED, []),
    )

    code = cli.main(["--repo", "o/r", "--pr", "7"])
    out = capsys.readouterr().out
    assert code == 0
    assert "A.lean: no declarations in the base version; nothing to check" in out
    assert "clean" not in out


def test_changed_verdict_exits_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    finding = Finding(
        decl="foo", base_statement="theorem foo : 1 = 1 :=", head_statement=None
    )
    monkeypatch.setattr(
        cli, "fetch_pr_files", lambda repo, pr: ("base", "head", ["A.lean"])
    )
    monkeypatch.setattr(cli, "_show", lambda sha, path: "")
    monkeypatch.setattr(
        cli,
        "compare_declarations",
        lambda base, head, *, profile: (Verdict.CHANGED, [finding]),
    )

    code = cli.main(["--repo", "o/r", "--pr", "7"])
    out = capsys.readouterr().out
    assert code == 1
    assert "foo" in out
    assert "spec D2" in out


def test_undetermined_verdict_exits_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """The semantic that differs from `statement_equiv`: here,
    UNDETERMINED fails rather than passing."""
    finding = Finding(
        decl="foo",
        base_statement="<duplicate declaration name — statement text not shown>",
        head_statement="<duplicate declaration name — statement text not shown>",
    )
    monkeypatch.setattr(
        cli, "fetch_pr_files", lambda repo, pr: ("base", "head", ["A.lean"])
    )
    monkeypatch.setattr(cli, "_show", lambda sha, path: "")
    monkeypatch.setattr(
        cli,
        "compare_declarations",
        lambda base, head, *, profile: (Verdict.UNDETERMINED, [finding]),
    )

    code = cli.main(["--repo", "o/r", "--pr", "7"])
    assert code == 1


def test_no_matching_files_exits_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        cli, "fetch_pr_files", lambda repo, pr: ("base", "head", ["README.md"])
    )

    code = cli.main(["--repo", "o/r", "--pr", "7"])
    out = capsys.readouterr().out
    assert code == 0
    assert "not applicable" in out.lower()


def test_external_failure_exits_two(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _raise(repo: str, pr: int) -> tuple[str, str, list[str]]:
        raise _ExternalError("gh pr view failed: not found")

    monkeypatch.setattr(cli, "fetch_pr_files", _raise)

    code = cli.main(["--repo", "o/r", "--pr", "7"])
    err = capsys.readouterr().err
    assert code == 2
    assert "gh pr view failed" in err


def test_multiple_files_any_failure_exits_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One clean file alongside one changed file still fails the audit."""
    finding = Finding(
        decl="bar", base_statement="theorem bar : 2 = 2 :=", head_statement=None
    )

    def _fetch(repo: str, pr: int) -> tuple[str, str, list[str]]:
        return "base", "head", ["A.lean", "B.lean"]

    def _compare(
        base: str, head: str, *, profile: object
    ) -> tuple[Verdict, list[Finding]]:
        if base == "A":
            return Verdict.UNCHANGED, []
        return Verdict.CHANGED, [finding]

    def _fake_show(sha: str, path: str) -> str:
        # Distinguish files by path so `_compare` can branch above.
        return "A" if path == "A.lean" else "B"

    monkeypatch.setattr(cli, "fetch_pr_files", _fetch)
    monkeypatch.setattr(cli, "_show", _fake_show)
    monkeypatch.setattr(cli, "compare_declarations", _compare)

    code = cli.main(["--repo", "o/r", "--pr", "7"])
    assert code == 1


def test_multiple_files_both_failing_aggregates_all_findings(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """F4: findings from EVERY failing file must appear, not just the
    first's — pins that the production loop doesn't `break`/`return`
    early or overwrite earlier findings with the last file's."""
    finding_a = Finding(
        decl="foo", base_statement="theorem foo : 1 = 1 :=", head_statement=None
    )
    finding_b = Finding(
        decl="bar", base_statement="theorem bar : 2 = 2 :=", head_statement=None
    )

    def _fetch(repo: str, pr: int) -> tuple[str, str, list[str]]:
        return "base", "head", ["A.lean", "B.lean"]

    def _compare(
        base: str, head: str, *, profile: object
    ) -> tuple[Verdict, list[Finding]]:
        if base == "A":
            return Verdict.CHANGED, [finding_a]
        return Verdict.CHANGED, [finding_b]

    def _fake_show(sha: str, path: str) -> str:
        return "A" if path == "A.lean" else "B"

    monkeypatch.setattr(cli, "fetch_pr_files", _fetch)
    monkeypatch.setattr(cli, "_show", _fake_show)
    monkeypatch.setattr(cli, "compare_declarations", _compare)

    code = cli.main(["--repo", "o/r", "--pr", "7"])
    out = capsys.readouterr().out
    assert code == 1
    assert "foo" in out
    assert "bar" in out


# ---------------------------------------------------------------------------
# F3 — an unresolvable prover exits 2, not a Python traceback
# ---------------------------------------------------------------------------


def test_unresolvable_prover_exits_two(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unknown `--prover` (or, by the same code path, a malformed
    `[project] prover` in the base-SHA `.choir/project.toml`) must not
    escape `main()` as an uncaught `ProverError` — that would print a
    raw traceback and exit with Python's default 1, which the exit
    contract reserves for "the contributor changed a statement.\""""
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


# ---------------------------------------------------------------------------
# format_findings — None (absent) vs "" (unparseable) vs real text
# ---------------------------------------------------------------------------


def test_format_findings_empty_list() -> None:
    assert "No base declarations were changed" in format_findings([])


def test_format_findings_distinguishes_absent_from_unparseable() -> None:
    deleted = Finding(decl="foo", base_statement="theorem foo : 1 = 1 :=", head_statement=None)
    unparseable_head = Finding(decl="bar", base_statement="axiom bar : Nat", head_statement="")
    changed = Finding(
        decl="baz", base_statement="theorem baz : 1 = 1 :=", head_statement="theorem baz : 1 = 2 :="
    )

    rendered = format_findings([deleted, unparseable_head, changed])

    assert "absent from head" in rendered
    assert "could not be extracted" in rendered
    assert "theorem baz : 1 = 2 :=" in rendered
    # The deleted finding's absence must never be confused with the
    # unparseable finding's empty string — different wording for each.
    deleted_line = next(line for line in rendered.splitlines() if "`foo`" in line)
    unparseable_line = next(line for line in rendered.splitlines() if "`bar`" in line)
    assert "absent" in deleted_line
    assert "absent" not in unparseable_line
