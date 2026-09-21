"""Tests for `gate.verify.comparator_cli` (design note 14 §6).

Explicit mode is exercised end-to-end against real git in a tmp repo
(the convention set by `test_statement_equiv_cli.py`); the PR-mode
gh plumbing mirrors `statement_equiv_cli` and is integration-tested
via the demo. The comparator binary itself is never run — invocation
is covered with a monkeypatched `subprocess.run`.
"""

from __future__ import annotations

import json
import subprocess
import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest

from gate.provers.lean4 import LEAN4
from gate.verify import comparator_cli
from gate.verify.comparator import CHALLENGE_PREFIX, Outcome
from gate.verify.comparator_cli import _is_reduction, main, run_comparator
from gate.verify.pr_files import PrFilesError

LAKEFILE = 'name = "proj"\nversion = "0.1.0"\n\n[[lean_lib]]\nname = "Proj"\n'
TOOLCHAIN = "leanprover/lean4:v4.31.0\n"
BASE_ROOT = "import Proj.Basic\n"
BASE_BASIC = "import Mathlib.Tactic\n\ntheorem tgt : True := by sorry\n"
HEAD_BASIC = "import Mathlib.Tactic\n\ntheorem tgt : True := by trivial\n"


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return result.stdout


def _commit_all(cwd: Path, msg: str) -> str:
    _git(cwd, "add", "-A")
    _git(cwd, "commit", "-q", "-m", msg)
    return _git(cwd, "rev-parse", "HEAD").strip()


def _make_repo(
    tmp_path: Path,
    *,
    lakefile: str = LAKEFILE,
    toolchain: str = TOOLCHAIN,
    base_extra: dict[str, str] | None = None,
    head_extra: dict[str, str] | None = None,
) -> tuple[Path, str, str]:
    """Repo with a base commit (sorried target) and a head commit (proof)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")

    (repo / "lakefile.toml").write_text(lakefile, encoding="utf-8")
    (repo / "lean-toolchain").write_text(toolchain, encoding="utf-8")
    (repo / "Proj.lean").write_text(BASE_ROOT, encoding="utf-8")
    (repo / "Proj").mkdir()
    (repo / "Proj" / "Basic.lean").write_text(BASE_BASIC, encoding="utf-8")
    for path, text in (base_extra or {}).items():
        full = repo / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(text, encoding="utf-8")
    base_sha = _commit_all(repo, "base")

    (repo / "Proj" / "Basic.lean").write_text(HEAD_BASIC, encoding="utf-8")
    for path, text in (head_extra or {}).items():
        full = repo / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(text, encoding="utf-8")
    head_sha = _commit_all(repo, "head")

    return repo, base_sha, head_sha


def _explicit_args(
    base_sha: str, head_sha: str, ws: Path, *extra: str
) -> list[str]:
    return [
        "--base-sha", base_sha,
        "--head-sha", head_sha,
        "--target-decl", "tgt",
        "--target-file", "Proj/Basic.lean",
        "--workspace-dir", str(ws),
        *extra,
    ]


def _args_with_bin(tmp_path: Path, monkeypatch) -> list[str]:  # type: ignore[no-untyped-def]
    """Explicit-mode args past the whole applicability chain, with a bin.

    Mirrors the setup `test_main_with_bin_reports_outcome_and_exits_zero`
    already used (repo + chdir + `--comparator-bin`), pulled out so the
    exit-code / report tests below don't repeat it.
    """
    repo, base_sha, head_sha = _make_repo(tmp_path)
    ws = tmp_path / "ws"
    monkeypatch.chdir(repo)
    return _explicit_args(base_sha, head_sha, ws, "--comparator-bin", "/fake/bin")


# ---------------------------------------------------------------------------
# explicit mode, generation only (no --comparator-bin)
# ---------------------------------------------------------------------------


def test_generation_only_exit_zero_and_workspace_contents(
    tmp_path: Path, monkeypatch, capsys
) -> None:  # type: ignore[no-untyped-def]
    repo, base_sha, head_sha = _make_repo(tmp_path)
    ws = tmp_path / "ws"
    monkeypatch.chdir(repo)

    assert main(_explicit_args(base_sha, head_sha, ws)) == 0
    out = capsys.readouterr().out
    assert "not-run" in out

    # Base tree, prefixed + import-rewritten.
    challenge_copy = (ws / CHALLENGE_PREFIX / "Proj.lean").read_text(encoding="utf-8")
    assert f"import {CHALLENGE_PREFIX}.Proj.Basic" in challenge_copy
    base_basic = (ws / CHALLENGE_PREFIX / "Proj" / "Basic.lean").read_text(
        encoding="utf-8"
    )
    assert "import Mathlib.Tactic" in base_basic  # external import untouched
    assert "sorry" in base_basic  # base version, not head

    # Head tree verbatim.
    head_basic = (ws / "Proj" / "Basic.lean").read_text(encoding="utf-8")
    assert "trivial" in head_basic
    assert CHALLENGE_PREFIX not in head_basic

    # Config files come from base.
    assert (ws / "lean-toolchain").read_text(encoding="utf-8") == TOOLCHAIN
    lakefile = (ws / "lakefile.toml").read_text(encoding="utf-8")
    assert lakefile.startswith(LAKEFILE)
    for lib in ("Challenge", "Solution", CHALLENGE_PREFIX):
        assert f'name = "{lib}"' in lakefile

    # Generated roots.
    assert (
        f"import {CHALLENGE_PREFIX}.Proj\n"
        in (ws / "Challenge.lean").read_text(encoding="utf-8")
    )
    assert "import Proj\n" in (ws / "Solution.lean").read_text(encoding="utf-8")
    assert (
        f"import {CHALLENGE_PREFIX}.Proj\n"
        == (ws / f"{CHALLENGE_PREFIX}.lean").read_text(encoding="utf-8")
    )

    config = json.loads((ws / "config.json").read_text(encoding="utf-8"))
    assert config["theorem_names"] == ["tgt"]
    assert config["permitted_axioms"] == ["propext", "Quot.sound", "Classical.choice"]
    assert config["enable_nanoda"] is False


def test_head_reserved_paths_are_skipped(
    tmp_path: Path, monkeypatch, capsys
) -> None:  # type: ignore[no-untyped-def]
    # A malicious head adding Challenge.lean / ChoirBase/* must not be able
    # to inject content into the trusted side of the workspace.
    repo, base_sha, head_sha = _make_repo(
        tmp_path,
        head_extra={
            "Challenge.lean": "-- evil challenge override\n",
            f"{CHALLENGE_PREFIX}/Hack.lean": "-- evil prefixed module\n",
        },
    )
    ws = tmp_path / "ws"
    monkeypatch.chdir(repo)

    assert main(_explicit_args(base_sha, head_sha, ws)) == 0
    assert "evil" not in (ws / "Challenge.lean").read_text(encoding="utf-8")
    assert not (ws / CHALLENGE_PREFIX / "Hack.lean").exists()


def test_report_sorry_policy_permits_sorry_axiom(
    tmp_path: Path, monkeypatch, capsys
) -> None:  # type: ignore[no-untyped-def]
    repo, base_sha, head_sha = _make_repo(
        tmp_path,
        base_extra={
            ".choir/verify.toml": '[audits.sorry_delta]\npolicy = "report"\n'
        },
    )
    ws = tmp_path / "ws"
    monkeypatch.chdir(repo)

    assert main(_explicit_args(base_sha, head_sha, ws)) == 0
    config = json.loads((ws / "config.json").read_text(encoding="utf-8"))
    assert "sorryAx" in config["permitted_axioms"]
    # .choir/ itself is never copied into the workspace sources.
    assert not (ws / ".choir").exists()
    assert not (ws / CHALLENGE_PREFIX / ".choir").exists()


# ---------------------------------------------------------------------------
# _is_reduction — whether the PR body declares a reduction of this target
# ---------------------------------------------------------------------------

_REDUCTION_BLOCK = (
    "```choir-reduction\n"
    "choir-reduction-version: 1\n"
    "parent: tgt\n"
    "children:\n"
    "  - decl: tgt.step\n"
    "```\n"
)

_PR_TASK_BODY = textwrap.dedent(
    """\
    ---
    choir-task-version: 1
    type: prove
    target_file: Proj/Basic.lean
    target_decl: tgt
    project_ref:
      repo: org/myproj
      commit: 1a2b3c4d
      toolchain: leanprover/lean4:v4.32.0
    deps: []
    ---

    ## Statement

    theorem tgt : True
    """
)


TARGET_FILE = "Proj/Basic.lean"


def _stub_pr_files(monkeypatch, files) -> None:  # type: ignore[no-untyped-def]
    """Answer `fetch_pr_files` with `files` as the PR's changed paths."""
    monkeypatch.setattr(
        comparator_cli,
        "fetch_pr_files",
        lambda repo, pr: ("base-sha", "head-sha", list(files)),
    )


def _reduction(body: str | None, *, target_decl: str = "tgt"):  # type: ignore[no-untyped-def]
    return _is_reduction(
        body,
        repo="org/myproj",
        pr=7,
        target_file=TARGET_FILE,
        target_decl=target_decl,
        profile=LEAN4,
    )


def test_is_reduction_true_for_a_block_naming_this_target(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _stub_pr_files(monkeypatch, [TARGET_FILE])
    assert _reduction(f"Closes #7\n\n{_REDUCTION_BLOCK}") == (True, None)


def test_is_reduction_false_for_a_pr_that_does_not_change_the_target_file(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    # The bypass this closes: a valid block naming the real target, on a
    # PR that touches no prover source at all. The target keeps whatever
    # placeholder base had — sorry-delta sees no delta to refuse — so
    # this audit must not permit `sorryAx` for it.
    _stub_pr_files(monkeypatch, ["README.md"])
    granted, message = _reduction(f"Closes #7\n\n{_REDUCTION_BLOCK}")
    assert granted is False
    assert message is not None
    assert TARGET_FILE in message


def test_is_reduction_false_with_no_block(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _stub_pr_files(monkeypatch, [TARGET_FILE])
    assert _reduction("an ordinary proof submission\n") == (False, None)


def test_is_reduction_false_for_a_malformed_block(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _stub_pr_files(monkeypatch, [TARGET_FILE])
    malformed = "```choir-reduction\nchoir-reduction-version: 9\n```\n"
    assert _reduction(malformed) == (False, None)


def test_is_reduction_false_when_parent_names_another_declaration(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    _stub_pr_files(monkeypatch, [TARGET_FILE])
    other = _REDUCTION_BLOCK.replace("parent: tgt", "parent: other")
    assert _reduction(other) == (False, None)


def test_is_reduction_false_in_explicit_mode_with_no_body() -> None:
    assert _reduction(None) == (False, None)


def test_an_ordinary_body_never_fetches_the_prs_changed_files(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # The fetch happens only once a block has named this target, so an
    # ordinary submission adds no API call to this audit.
    def boom(repo: str, pr: int):  # type: ignore[no-untyped-def]
        raise AssertionError("fetch_pr_files called for an ordinary body")

    monkeypatch.setattr(comparator_cli, "fetch_pr_files", boom)
    assert _reduction("an ordinary proof submission\n") == (False, None)


def _stub_gh_pr_mode(  # type: ignore[no-untyped-def]
    monkeypatch, *, pr_meta: dict, issue_body: str = "", changed_files=(TARGET_FILE,)
):
    """Answer `gh pr view` with `pr_meta` as JSON, `gh issue view` with
    `issue_body`, and `fetch_pr_files` with `changed_files`."""

    def fake_gh(*args: str) -> str:
        return json.dumps(pr_meta) if args[0] == "pr" else issue_body

    monkeypatch.setattr(comparator_cli, "_gh", fake_gh)
    _stub_pr_files(monkeypatch, changed_files)


def test_pr_mode_with_a_valid_reduction_permits_sorry_axiom(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    repo, base_sha, head_sha = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    ws = tmp_path / "ws"
    _stub_gh_pr_mode(
        monkeypatch,
        pr_meta={
            "body": f"Closes #7\n\n{_REDUCTION_BLOCK}",
            "baseRefOid": base_sha,
            "headRefOid": head_sha,
        },
        issue_body=_PR_TASK_BODY,
    )
    assert main(["--repo", "org/myproj", "--pr", "7", "--workspace-dir", str(ws)]) == 0
    config = json.loads((ws / "config.json").read_text(encoding="utf-8"))
    assert "sorryAx" in config["permitted_axioms"]


def test_pr_mode_with_no_reduction_block_refuses_sorry_axiom(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    repo, base_sha, head_sha = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    ws = tmp_path / "ws"
    _stub_gh_pr_mode(
        monkeypatch,
        pr_meta={
            "body": "Closes #7\n\nan ordinary proof submission\n",
            "baseRefOid": base_sha,
            "headRefOid": head_sha,
        },
        issue_body=_PR_TASK_BODY,
    )
    assert main(["--repo", "org/myproj", "--pr", "7", "--workspace-dir", str(ws)]) == 0
    config = json.loads((ws / "config.json").read_text(encoding="utf-8"))
    assert "sorryAx" not in config["permitted_axioms"]


def test_pr_mode_with_a_block_on_a_pr_changing_no_source_refuses_sorry_axiom(
    tmp_path: Path, monkeypatch, capsys
) -> None:  # type: ignore[no-untyped-def]
    # The bypass this closes, end to end: the block is valid, its parent
    # is the real target, the link is honest — and the PR changes no
    # prover source file, so the target still carries base's
    # placeholder. sorry-delta has no delta to refuse there, which
    # leaves this audit as the only thing that can, so `sorryAx` must
    # stay out of the whitelist.
    repo, base_sha, head_sha = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    ws = tmp_path / "ws"
    _stub_gh_pr_mode(
        monkeypatch,
        pr_meta={
            "body": f"Closes #7\n\n{_REDUCTION_BLOCK}",
            "baseRefOid": base_sha,
            "headRefOid": head_sha,
        },
        issue_body=_PR_TASK_BODY,
        changed_files=("README.md",),
    )
    assert main(["--repo", "org/myproj", "--pr", "7", "--workspace-dir", str(ws)]) == 0
    config = json.loads((ws / "config.json").read_text(encoding="utf-8"))
    assert "sorryAx" not in config["permitted_axioms"]
    assert "not among the files this PR changes" in capsys.readouterr().out


def test_pr_mode_changed_file_fetch_failure_is_infrastructure(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    # An unfetched file list is an unknown diff, not an empty one: the
    # run exits 2 rather than deciding the whitelist either way.
    repo, base_sha, head_sha = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    _stub_gh_pr_mode(
        monkeypatch,
        pr_meta={
            "body": f"Closes #7\n\n{_REDUCTION_BLOCK}",
            "baseRefOid": base_sha,
            "headRefOid": head_sha,
        },
        issue_body=_PR_TASK_BODY,
    )

    def boom(repo: str, pr: int):  # type: ignore[no-untyped-def]
        raise PrFilesError("gh api failed")

    monkeypatch.setattr(comparator_cli, "fetch_pr_files", boom)
    assert main(["--repo", "org/myproj", "--pr", "7"]) == 2


def test_pr_mode_gh_failure_resolving_the_target_is_infrastructure(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    def boom(*args: str) -> str:
        raise comparator_cli._ExternalError("gh pr view failed")

    monkeypatch.setattr(comparator_cli, "_gh", boom)
    assert main(["--repo", "org/myproj", "--pr", "7"]) == 2


# ---------------------------------------------------------------------------
# applicability chain — informational passes (exit 0)
# ---------------------------------------------------------------------------


def test_non_lean4_prover_not_applicable(
    tmp_path: Path, monkeypatch, capsys
) -> None:  # type: ignore[no-untyped-def]
    repo, base_sha, head_sha = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    args = _explicit_args(base_sha, head_sha, tmp_path / "ws", "--prover", "isabelle")
    assert main(args) == 0
    assert "not-applicable" in capsys.readouterr().out


def test_below_floor_toolchain_not_applicable(
    tmp_path: Path, monkeypatch, capsys
) -> None:  # type: ignore[no-untyped-def]
    repo, base_sha, head_sha = _make_repo(
        tmp_path, toolchain="leanprover/lean4:v4.14.0\n"
    )
    monkeypatch.chdir(repo)
    assert main(_explicit_args(base_sha, head_sha, tmp_path / "ws")) == 0
    out = capsys.readouterr().out
    assert "not-applicable" in out
    assert "toolchain" in out


def test_reserved_lib_collision_not_applicable(
    tmp_path: Path, monkeypatch, capsys
) -> None:  # type: ignore[no-untyped-def]
    repo, base_sha, head_sha = _make_repo(
        tmp_path, lakefile=LAKEFILE + '\n[[lean_lib]]\nname = "Challenge"\n'
    )
    monkeypatch.chdir(repo)
    assert main(_explicit_args(base_sha, head_sha, tmp_path / "ws")) == 0
    assert "not-applicable" in capsys.readouterr().out


def test_target_file_absent_at_base_not_applicable(
    tmp_path: Path, monkeypatch, capsys
) -> None:  # type: ignore[no-untyped-def]
    repo, base_sha, head_sha = _make_repo(
        tmp_path, head_extra={"Proj/New.lean": "theorem other : True := trivial\n"}
    )
    monkeypatch.chdir(repo)
    args = [
        "--base-sha", base_sha,
        "--head-sha", head_sha,
        "--target-decl", "other",
        "--target-file", "Proj/New.lean",
        "--workspace-dir", str(tmp_path / "ws"),
    ]
    assert main(args) == 0
    out = capsys.readouterr().out
    assert "not-applicable" in out
    assert "base" in out


# ---------------------------------------------------------------------------
# infrastructure errors (exit 2)
# ---------------------------------------------------------------------------


def test_missing_lakefile_is_infra_error(
    tmp_path: Path, monkeypatch, capsys
) -> None:  # type: ignore[no-untyped-def]
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "lean-toolchain").write_text(TOOLCHAIN, encoding="utf-8")
    (repo / "Proj").mkdir()
    (repo / "Proj" / "Basic.lean").write_text(BASE_BASIC, encoding="utf-8")
    base_sha = _commit_all(repo, "base (no lakefile)")
    (repo / "Proj" / "Basic.lean").write_text(HEAD_BASIC, encoding="utf-8")
    head_sha = _commit_all(repo, "head")

    monkeypatch.chdir(repo)
    assert main(_explicit_args(base_sha, head_sha, tmp_path / "ws")) == 2


# ---------------------------------------------------------------------------
# run_comparator classification (invocation monkeypatched)
# ---------------------------------------------------------------------------


def _fake_run(returncode: int, stdout: str = "", stderr: str = ""):  # type: ignore[no-untyped-def]
    def fake(cmd, **kwargs):  # type: ignore[no-untyped-def]
        return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)

    return fake


@dataclass
class _FakeProc:
    stdout: str
    stderr: str = ""
    returncode: int = 0


def test_run_comparator_classifies_illegal_axiom(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        comparator_cli.subprocess,
        "run",
        _fake_run(1, stderr="Illegal axiom detected: 'Cheat.ax'"),
    )
    outcome, message, _ = run_comparator(tmp_path, "/nonexistent/comparator")
    assert outcome is Outcome.ILLEGAL_AXIOM
    assert "Cheat.ax" in message


def test_run_comparator_match(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        comparator_cli.subprocess,
        "run",
        _fake_run(0, stdout="Your solution is okay!"),
    )
    outcome, _, _ = run_comparator(tmp_path, "/nonexistent/comparator")
    assert outcome is Outcome.MATCH


def test_run_comparator_reports_failed_cache_get(tmp_path, monkeypatch, capsys):  # type: ignore[no-untyped-def]
    (tmp_path / "lake-manifest.json").write_text('{"packages":[{"name":"mathlib"}]}')

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(cmd)
        if cmd[:4] == ["lake", "exe", "cache", "get"]:
            return _FakeProc(stdout="", stderr="cache server unreachable", returncode=1)
        return _FakeProc(stdout="comparator accepted", stderr="", returncode=0)

    monkeypatch.setattr(comparator_cli.subprocess, "run", fake_run)
    outcome, _message, _full = comparator_cli.run_comparator(tmp_path, "cmp")

    assert outcome is Outcome.MATCH
    # a failed cache get means the solution build fell back to source; say so
    assert "cache get failed" in capsys.readouterr().out


def test_main_with_bin_reports_outcome_and_exits_one_on_worker_failure(
    tmp_path: Path, monkeypatch, capsys
) -> None:  # type: ignore[no-untyped-def]
    # Renamed from ..._exits_zero: under the exit-code contract a
    # statement-mismatch is the worker's audit failure, so it now exits 1
    # rather than 0. The report-contains-the-outcome assertion is unchanged.
    args = _args_with_bin(tmp_path, monkeypatch)
    monkeypatch.setattr(
        comparator_cli,
        "run_comparator",
        lambda ws_dir, bin_path: (Outcome.STATEMENT_MISMATCH, "statement differs", ""),
    )
    assert main(args) == 1
    out = capsys.readouterr().out
    assert "statement-mismatch" in out


# ---------------------------------------------------------------------------
# exit codes + whose-problem-is-it reporting
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "outcome,expected_code",
    [
        (Outcome.MATCH, 0),
        (Outcome.STATEMENT_MISMATCH, 1),
        (Outcome.ILLEGAL_AXIOM, 1),
        (Outcome.SOLUTION_BUILD_FAILED, 1),
        (Outcome.SANDBOX_UNAVAILABLE, 2),
        (Outcome.TOOL_ERROR, 2),
        # missing-constant is infrastructure, not the contributor: the
        # challenge environment is base tree + Choir's generator, so no head
        # tree can change which constants it holds. It is reachable via a
        # mis-scoped task whose target_decl is absent at base (applicability
        # gates on the target *file*), and exit 1 there would put a red
        # "you did this" check on a PR that did nothing wrong.
        (Outcome.MISSING_CONSTANT, 2),
    ],
)
def test_exit_code_per_outcome(
    tmp_path: Path, monkeypatch, outcome: Outcome, expected_code: int
) -> None:  # type: ignore[no-untyped-def]
    args = _args_with_bin(tmp_path, monkeypatch)
    monkeypatch.setattr(
        comparator_cli,
        "run_comparator",
        lambda ws, bin: (outcome, "message", "full transcript"),
    )
    assert main(args) == expected_code


def test_missing_constant_is_reported_as_infrastructure(
    tmp_path: Path, monkeypatch, capsys
) -> None:  # type: ignore[no-untyped-def]
    """The report must not blame the contributor for a missing constant.

    The exit code alone is not enough: a human reading CI output sees the
    note, so it has to say "infrastructure", not "audit failure".
    """
    args = _args_with_bin(tmp_path, monkeypatch)
    monkeypatch.setattr(
        comparator_cli,
        "run_comparator",
        lambda ws, bin: (
            Outcome.MISSING_CONSTANT,
            "Const not found in challenge",
            "",
        ),
    )
    assert main(args) == 2
    out = capsys.readouterr().out.lower()
    assert "infrastructure" in out
    assert "escalat" in out


def test_every_outcome_is_deliberately_classified() -> None:
    """Every `Outcome` member must be in exactly one exit-code bucket.

    `_report_outcome` falls through both frozensets to `return 0`, so a new
    member added without touching them would silently make the check green
    having verified nothing — reinstating the decorative-green bug this
    slice exists to kill. This repo already has the scar: `gate/checks.py`
    exists because a check name was added to one list and not another.

    A new member therefore has to be classified on purpose: worker-owned
    (exit 1), cannot-verify (exit 2), or `MATCH` (exit 0).
    """
    classified = (
        comparator_cli._WORKER_FAILURES
        | comparator_cli._INFRA_FAILURES
        | {Outcome.MATCH}
    )
    assert set(Outcome) == classified
    assert not (comparator_cli._WORKER_FAILURES & comparator_cli._INFRA_FAILURES)


def test_oserror_from_the_invocation_exits_two_not_one(
    tmp_path: Path, monkeypatch, capsys
) -> None:  # type: ignore[no-untyped-def]
    """A missing `lake` is infrastructure; exit 1 means "the PR owns this"."""
    args = _args_with_bin(tmp_path, monkeypatch)

    def boom(ws, bin):  # type: ignore[no-untyped-def]
        raise FileNotFoundError(2, "No such file or directory: 'lake'")

    monkeypatch.setattr(comparator_cli, "run_comparator", boom)
    assert main(args) == 2
    assert "comparator invocation failed" in capsys.readouterr().err


def test_solution_build_failure_names_rebuild_as_the_root_cause(
    tmp_path: Path, monkeypatch, capsys
) -> None:  # type: ignore[no-untyped-def]
    args = _args_with_bin(tmp_path, monkeypatch)
    monkeypatch.setattr(
        comparator_cli,
        "run_comparator",
        lambda ws, bin: (
            Outcome.SOLUTION_BUILD_FAILED,
            "error: build failed",
            "./Foo.lean:12:4: error: ring_nf made no progress\nerror: build failed\n",
        ),
    )
    code = main(args)
    out = capsys.readouterr().out
    assert code == 1
    assert "rebuild" in out  # points at the check that owns the cause
    assert "ring_nf made no progress" in out  # transcript survives
    # header names the actual outcome, not a hardcoded "(tool error)"
    assert "--- comparator output (solution-build-failed) ---" in out
    # The attribution is hedged, not asserted: nothing in the transcript says
    # *which* library failed, so a green `rebuild` means the fault is in
    # comparator's own generated workspace.
    assert "GREEN" in out
    assert "escalate" in out


def test_solution_build_failure_flags_a_challenge_side_error(
    tmp_path: Path, monkeypatch, capsys
) -> None:  # type: ignore[no-untyped-def]
    """An error under `ChoirBase/` is Choir's workspace, not the PR's diff."""
    args = _args_with_bin(tmp_path, monkeypatch)
    monkeypatch.setattr(
        comparator_cli,
        "run_comparator",
        lambda ws, bin: (
            Outcome.SOLUTION_BUILD_FAILED,
            "error: build failed",
            f"./{CHALLENGE_PREFIX}/Proj/Basic.lean:1:0: error: unknown module\n"
            "error: build failed\n",
        ),
    )
    assert main(args) == 1
    out = capsys.readouterr().out
    assert f"error under {CHALLENGE_PREFIX}/" in out
    assert "workspace-generation fault" in out


def test_solution_build_failure_does_not_flag_challenge_side_on_head_error(
    tmp_path: Path, monkeypatch, capsys
) -> None:  # type: ignore[no-untyped-def]
    """A sorry warning under `ChoirBase/` must not read as a generation fault.

    The challenge tree carries the task's sorried target by construction, so
    Lean warns about it on every single run — flagging on a bare mention of
    the prefix would fire on every PR.
    """
    args = _args_with_bin(tmp_path, monkeypatch)
    monkeypatch.setattr(
        comparator_cli,
        "run_comparator",
        lambda ws, bin: (
            Outcome.SOLUTION_BUILD_FAILED,
            "error: build failed",
            f"./{CHALLENGE_PREFIX}/Proj/Basic.lean:3:8: warning: declaration "
            "uses 'sorry'\n"
            "./Proj/Basic.lean:3:20: error: unknown identifier 'foo'\n"
            "error: build failed\n",
        ),
    )
    assert main(args) == 1
    out = capsys.readouterr().out
    assert f"error under {CHALLENGE_PREFIX}/" not in out


def test_sandbox_unavailable_is_reported_as_infrastructure(
    tmp_path: Path, monkeypatch, capsys
) -> None:  # type: ignore[no-untyped-def]
    args = _args_with_bin(tmp_path, monkeypatch)
    monkeypatch.setattr(
        comparator_cli,
        "run_comparator",
        lambda ws, bin: (Outcome.SANDBOX_UNAVAILABLE, "no landrun", "transcript"),
    )
    code = main(args)
    out = capsys.readouterr().out.lower()
    assert code == 2
    assert "infrastructure" in out
    # must not read as the contributor's fault
    assert "escalat" in out
