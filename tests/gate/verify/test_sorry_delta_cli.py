"""Tests for `gate.verify.sorry_delta_cli` policy handling.

The full CLI dispatch (gh against a remote PR) is exercised in the
demo; these tests pin down the policy-critical parts:

- the sorry policy must come from the *base* SHA, not the PR head —
  otherwise a contributor could flip the project to `report` mode in
  their own PR;
- the exit-code mapping: `block` fails on net-new sorries (the merge
  blocker), `report` prints them but passes — the orchestrator's
  review, not the gate, is then the catch-point;
- the reduction contract's provenance: its target comes from the linked
  task, and every incomplete or disagreeing reading falls back to the
  ordinary contract instead of relaxing anything.
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest

from gate.provers.isabelle import ISABELLE
from gate.provers.lean4 import LEAN4
from gate.provers.rocq import ROCQ
from gate.verify import sorry_delta_cli
from gate.verify.config import SorryPolicy
from gate.verify.sorry_delta_cli import (
    _resolve_reduction,
    exit_code_for,
    filter_files_by_profile,
    load_verify_config_from_base,
    main,
)


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return result.stdout


def _init_repo_with_policy_history(tmp_path: Path) -> tuple[str, str]:
    """Set up a tmp git repo:

    - base SHA: `.choir/verify.toml` declares `policy = "block"`.
    - head SHA: same file flipped to `report` (simulates a PR trying
      to loosen the sorry gate on itself).
    """
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / ".choir").mkdir()
    (tmp_path / ".choir" / "verify.toml").write_text(
        '[audits.sorry_delta]\npolicy = "block"\n',
        encoding="utf-8",
    )
    _git(tmp_path, "add", ".choir/verify.toml")
    _git(tmp_path, "commit", "-q", "-m", "base: maintainer policy")
    base_sha = _git(tmp_path, "rev-parse", "HEAD").strip()

    (tmp_path / ".choir" / "verify.toml").write_text(
        '[audits.sorry_delta]\npolicy = "report"\n',
        encoding="utf-8",
    )
    _git(tmp_path, "add", ".choir/verify.toml")
    _git(tmp_path, "commit", "-q", "-m", "head: attempt to loosen policy")
    head_sha = _git(tmp_path, "rev-parse", "HEAD").strip()

    return base_sha, head_sha


def test_loads_sorry_policy_from_base(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    base_sha, _head = _init_repo_with_policy_history(tmp_path)
    monkeypatch.chdir(tmp_path)
    cfg = load_verify_config_from_base(base_sha)
    assert cfg.sorry_delta.policy is SorryPolicy.BLOCK


def test_ignores_head_policy_loosening(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # The whole point of base-SHA reads: the head commit's flip to
    # `report` is irrelevant; we pass base_sha.
    base_sha, _head = _init_repo_with_policy_history(tmp_path)
    monkeypatch.chdir(tmp_path)
    cfg = load_verify_config_from_base(base_sha)
    assert cfg.sorry_delta.policy is not SorryPolicy.REPORT


def test_block_policy_fails_on_introduced_sorries() -> None:
    assert exit_code_for(any_introduced=True, policy=SorryPolicy.BLOCK) == 1


def test_report_policy_passes_on_introduced_sorries() -> None:
    assert exit_code_for(any_introduced=True, policy=SorryPolicy.REPORT) == 0


def test_clean_passes_under_both_policies() -> None:
    assert exit_code_for(any_introduced=False, policy=SorryPolicy.BLOCK) == 0
    assert exit_code_for(any_introduced=False, policy=SorryPolicy.REPORT) == 0


# ---------------------------------------------------------------------------
# filter_files_by_profile — per-prover extension filtering
# ---------------------------------------------------------------------------


def test_filter_files_lean4_keeps_only_lean(tmp_path: Path) -> None:
    files = ["A.lean", "README.md", "sub/B.lean", "notes.txt"]
    assert filter_files_by_profile(files, LEAN4) == ["A.lean", "sub/B.lean"]


def test_filter_files_isabelle_keeps_only_thy() -> None:
    files = ["Scratch.thy", "A.lean", "ROOT"]
    assert filter_files_by_profile(files, ISABELLE) == ["Scratch.thy"]


def test_filter_files_rocq_keeps_only_v() -> None:
    files = ["Scratch.v", "A.lean", "_CoqProject"]
    assert filter_files_by_profile(files, ROCQ) == ["Scratch.v"]


# ---------------------------------------------------------------------------
# _resolve_reduction — where the reduction's target comes from
# ---------------------------------------------------------------------------

_TARGET_FILE = "MyProj/Foo.lean"
_OTHER_FILE = "MyProj/Bar.lean"

_BLOCK = (
    "```choir-reduction\n"
    "choir-reduction-version: 1\n"
    "parent: MyProj.Foo.add_comm\n"
    "children:\n"
    "  - decl: MyProj.Foo.step\n"
    "  - decl: MyProj.Foo.step2\n"
    "```\n"
)

_TASK_BODY = textwrap.dedent(
    """\
    ---
    choir-task-version: 1
    type: prove
    target_file: MyProj/Foo.lean
    target_decl: MyProj.Foo.add_comm
    project_ref:
      repo: org/myproj
      commit: 1a2b3c4d
      toolchain: leanprover/lean4:v4.32.0
    deps: []
    ---

    ## Statement

    theorem MyProj.Foo.add_comm ...
    """
)

# The shape every reduction of a pre-existing target has: the target's
# statement is untouched (statement-immutability requires that) and only
# its proof body moves, from a placeholder onto two freshly declared,
# sorried obligations. Base 1 placeholder, head 2, so the ordinary
# contract refuses this diff and the reduction contract does not.
_BASE_FILE = (
    "namespace MyProj.Foo\n"
    "\n"
    "theorem add_comm : True := by\n"
    "  sorry\n"
    "\n"
    "end MyProj.Foo\n"
)
_HEAD_FILE = (
    "namespace MyProj.Foo\n"
    "\n"
    "theorem step : True := by\n"
    "  sorry\n"
    "\n"
    "theorem step2 : True := by\n"
    "  sorry\n"
    "\n"
    "theorem add_comm : True := step\n"
    "\n"
    "end MyProj.Foo\n"
)


_PR_VIEW_CALL = (
    "gh", "pr", "view", "3", "--repo", "org/myproj",
    "--json", "body", "--jq", ".body",
)


def _stub_gh(  # type: ignore[no-untyped-def]
    monkeypatch, *, pr_body: str = "", issue_body: str = ""
) -> list[tuple[str, ...]]:
    """Answer `gh pr view` with `pr_body`, `gh issue view` with
    `issue_body`; leave `git` real. Logs every `gh` call."""
    real_run = sorry_delta_cli._run
    calls: list[tuple[str, ...]] = []

    def fake_run(tool: str, *args: str) -> str:
        if tool != "gh":
            return real_run(tool, *args)
        calls.append((tool, *args))
        return pr_body if args[0] == "pr" else issue_body

    monkeypatch.setattr(sorry_delta_cli, "_run", fake_run)
    return calls


def _resolve(*, changed_files=(_TARGET_FILE,)):  # type: ignore[no-untyped-def]
    return _resolve_reduction(repo="org/myproj", pr=3, changed_files=changed_files)


def test_a_body_without_a_block_means_no_reduction(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _stub_gh(monkeypatch, pr_body="an ordinary proof submission\n")
    reduction, message = _resolve()
    assert reduction is None
    assert message is None


def test_a_malformed_block_grants_nothing(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _stub_gh(
        monkeypatch,
        pr_body="```choir-reduction\nchoir-reduction-version: 9\n```\n",
    )
    reduction, message = _resolve()
    assert reduction is None
    assert message is not None


def test_a_block_agreeing_with_the_task_is_resolved(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    gh_calls = _stub_gh(
        monkeypatch, pr_body=f"Closes #7\n\n{_BLOCK}", issue_body=_TASK_BODY
    )
    reduction, message = _resolve()
    assert message is None
    assert reduction is not None
    assert reduction.target_file == _TARGET_FILE
    assert reduction.target_decl == "MyProj.Foo.add_comm"
    assert reduction.children == ("MyProj.Foo.step", "MyProj.Foo.step2")
    # The target is read off the linked issue, not the block.
    assert gh_calls == [
        _PR_VIEW_CALL,
        (
            "gh", "issue", "view", "7", "--repo", "org/myproj",
            "--json", "body", "-q", ".body",
        ),
    ]


def test_a_parent_that_is_not_the_tasks_target_grants_nothing(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # The block names a declaration the task did not scope. Nothing is
    # granted: a submitter free to name the target would name a helper
    # and leave the real one sorried.
    block = _BLOCK.replace("parent: MyProj.Foo.add_comm", "parent: MyProj.Foo.helper")
    _stub_gh(monkeypatch, pr_body=f"Closes #7\n\n{block}", issue_body=_TASK_BODY)
    reduction, message = _resolve()
    assert reduction is None
    assert message is not None
    assert "MyProj.Foo.helper" in message
    assert "MyProj.Foo.add_comm" in message


def test_a_task_whose_target_file_this_pr_does_not_change_grants_nothing(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # A closing reference is body text, so a PR can name a task other
    # than the one it works on. The linked task's target file is not
    # among the PR's files, so the link does not describe this
    # submission.
    _stub_gh(monkeypatch, pr_body=f"Closes #7\n\n{_BLOCK}", issue_body=_TASK_BODY)
    reduction, message = _resolve(changed_files=(_OTHER_FILE,))
    assert reduction is None
    assert message is not None
    assert _TARGET_FILE in message


def test_a_body_linking_no_issue_grants_nothing(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    gh_calls = _stub_gh(monkeypatch, pr_body=_BLOCK, issue_body=_TASK_BODY)
    reduction, message = _resolve()
    assert reduction is None
    assert message is not None
    # No closing reference in the body, so the issue is never fetched.
    assert gh_calls == [_PR_VIEW_CALL]


def test_a_linked_issue_that_does_not_parse_grants_nothing(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _stub_gh(
        monkeypatch,
        pr_body=f"Closes #7\n\n{_BLOCK}",
        issue_body="just prose, with no front matter\n",
    )
    reduction, message = _resolve()
    assert reduction is None
    assert message is not None


def test_a_linked_task_without_a_target_decl_grants_nothing(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _stub_gh(
        monkeypatch,
        pr_body=f"Closes #7\n\n{_BLOCK}",
        issue_body=_TASK_BODY.replace("target_decl: MyProj.Foo.add_comm\n", ""),
    )
    reduction, message = _resolve()
    assert reduction is None
    assert message is not None


def test_a_gh_failure_fetching_the_pr_body_is_not_a_fallback(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # An unfetched body is an unknown submission, not an ordinary one —
    # the failure propagates to `main`'s infrastructure exit rather than
    # quietly auditing under the ordinary contract.
    def boom(tool: str, *args: str) -> str:
        raise sorry_delta_cli._ExternalError("gh pr view failed")

    monkeypatch.setattr(sorry_delta_cli, "_run", boom)
    with pytest.raises(sorry_delta_cli._ExternalError):
        _resolve()


def test_a_gh_failure_resolving_the_linked_issue_is_not_a_fallback(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # An unread issue is an unknown target, not an absent one, so the
    # failure propagates to `main`'s infrastructure exit rather than
    # quietly auditing under the ordinary contract.
    real_run = sorry_delta_cli._run

    def fake_run(tool: str, *args: str) -> str:
        if tool == "gh" and args[0] == "pr":
            return f"Closes #7\n\n{_BLOCK}"
        if tool == "gh" and args[0] == "issue":
            raise sorry_delta_cli._ExternalError("gh issue view failed")
        return real_run(tool, *args)

    monkeypatch.setattr(sorry_delta_cli, "_run", fake_run)
    with pytest.raises(sorry_delta_cli._ExternalError):
        _resolve()


# ---------------------------------------------------------------------------
# main — which contract the audit runs, over which files, against real git
# ---------------------------------------------------------------------------


def _make_repo(
    tmp_path: Path, base: dict[str, str], head: dict[str, str]
) -> tuple[Path, str, str]:
    """A real repo with a base and a head commit; `(path, base_sha, head_sha)`."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    for contents, message in ((base, "base"), (head, "head")):
        for path, text in contents.items():
            dest = repo / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(text, encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", message)
    head_sha = _git(repo, "rev-parse", "HEAD").strip()
    base_sha = _git(repo, "rev-parse", "HEAD~1").strip()
    return repo, base_sha, head_sha


def _stub_audit(  # type: ignore[no-untyped-def]
    monkeypatch,
    tmp_path: Path,
    issue_body: str,
    *,
    pr_body: str = "",
    base: dict[str, str] | None = None,
    head: dict[str, str] | None = None,
    changed_files: tuple[str, ...] | None = None,
) -> None:
    """Run `main` against a real git repo, stubbing only the PR API.

    Base and head contents become two real commits that `main` reads
    back through its own `git show`. `.choir` config is absent, so the
    prover resolves to lean4 and the policy to `block`, both through the
    real resolvers. Only `fetch_pr_files` and `gh` (the PR-body and
    issue-body lookups) are stubbed — there is no pull request to ask
    about. `pr_body` defaults to empty, i.e. no reduction block, and
    `changed_files` defaults to every path either commit writes.
    """
    base_files = {_TARGET_FILE: _BASE_FILE} if base is None else base
    head_files = {_TARGET_FILE: _HEAD_FILE} if head is None else head
    repo, base_sha, head_sha = _make_repo(tmp_path, base_files, head_files)
    monkeypatch.chdir(repo)
    paths = (
        sorted(set(base_files) | set(head_files))
        if changed_files is None
        else list(changed_files)
    )
    monkeypatch.setattr(
        sorry_delta_cli, "fetch_pr_files", lambda repo, pr: (base_sha, head_sha, paths)
    )
    _stub_gh(monkeypatch, pr_body=pr_body, issue_body=issue_body)


def _audit() -> int:
    return main(["--repo", "org/myproj", "--pr", "3"])


def test_an_honest_reduction_of_a_pre_existing_target_is_clean(
    tmp_path: Path, monkeypatch, capsys  # type: ignore[no-untyped-def]
) -> None:
    # The shape of every reduction of a target the corpus already has:
    # the target's statement is byte-identical across the diff and only
    # its proof body moves, onto two freshly declared, sorried
    # obligations in the same file. No statement-level detector can see
    # that edit at all, so the link has to be bound by something else,
    # and the audit has to read this as a reduction rather than as
    # net-new placeholders.
    _stub_audit(monkeypatch, tmp_path, _TASK_BODY, pr_body=f"Closes #7\n\n{_BLOCK}")
    code = _audit()
    out = capsys.readouterr().out
    assert code == 0
    assert "submission: reduction" in out
    assert f"reduction target: MyProj.Foo.add_comm in {_TARGET_FILE}" in out
    assert f"{_TARGET_FILE}: clean" in out


def test_the_same_diff_without_a_block_gets_the_ordinary_contract(
    tmp_path: Path, monkeypatch, capsys  # type: ignore[no-untyped-def]
) -> None:
    # The control for the test above: the identical diff read under the
    # ordinary contract is two placeholders against one, and is refused.
    # Without it, a clean reduction verdict could mean the fixture was
    # clean either way.
    _stub_audit(monkeypatch, tmp_path, _TASK_BODY)
    code = _audit()
    out = capsys.readouterr().out
    assert code == 1
    assert "submission: proof" in out
    assert f"{_TARGET_FILE}: net-new placeholders" in out


def test_a_block_on_a_pr_changing_no_source_gets_the_ordinary_contract(
    tmp_path: Path, monkeypatch, capsys  # type: ignore[no-untyped-def]
) -> None:
    # The bypass this closes: a valid block whose parent is the real
    # target, an honest link, and a PR that changes no prover source
    # file at all. The target keeps base's placeholder, so there is no
    # delta for this audit to refuse — and it must not read the
    # submission as a reduction either, since `comparator` grants
    # `sorryAx` for exactly the submissions this audit calls reductions.
    _stub_audit(
        monkeypatch,
        tmp_path,
        _TASK_BODY,
        pr_body=f"Closes #7\n\n{_BLOCK}",
        base={_TARGET_FILE: _BASE_FILE, "README.md": "before\n"},
        head={_TARGET_FILE: _BASE_FILE, "README.md": "after\n"},
        changed_files=("README.md",),
    )
    code = _audit()
    out = capsys.readouterr().out
    assert code == 0
    assert "submission: proof" in out
    assert "not among the files this PR changes" in out


def test_a_target_file_outside_the_provers_profile_gets_the_ordinary_contract(
    tmp_path: Path, monkeypatch, capsys  # type: ignore[no-untyped-def]
) -> None:
    # The contract is granted from the same list the audit loop
    # iterates: the changed files this prover's profile scans. A task
    # naming a target file the profile does not scan is one intake
    # refuses at publish time and this audit does not re-check, so the
    # shared list is what keeps the grant and the loop from disagreeing
    # about which files exist.
    _stub_audit(
        monkeypatch,
        tmp_path,
        _TASK_BODY.replace("MyProj/Foo.lean", "MyProj/Foo.thy"),
        pr_body=f"Closes #7\n\n{_BLOCK}",
        changed_files=("MyProj/Foo.thy",),
    )
    code = _audit()
    out = capsys.readouterr().out
    assert code == 0
    assert "submission: proof" in out
    assert "MyProj/Foo.thy" in out


def test_a_block_linking_no_issue_gets_the_ordinary_contract(
    tmp_path: Path, monkeypatch, capsys  # type: ignore[no-untyped-def]
) -> None:
    _stub_audit(monkeypatch, tmp_path, _TASK_BODY, pr_body=_BLOCK)
    code = _audit()
    out = capsys.readouterr().out
    assert code == 1
    assert "submission: proof" in out
    assert "links no issue" in out


def test_a_block_whose_task_does_not_parse_gets_the_ordinary_contract(
    tmp_path: Path, monkeypatch, capsys  # type: ignore[no-untyped-def]
) -> None:
    _stub_audit(
        monkeypatch,
        tmp_path,
        "just prose, with no front matter\n",
        pr_body=f"Closes #7\n\n{_BLOCK}",
    )
    code = _audit()
    out = capsys.readouterr().out
    assert code == 1
    assert "submission: proof" in out
    assert "does not parse" in out


def test_a_block_naming_another_declaration_gets_the_ordinary_contract(
    tmp_path: Path, monkeypatch, capsys  # type: ignore[no-untyped-def]
) -> None:
    block = _BLOCK.replace("parent: MyProj.Foo.add_comm", "parent: MyProj.Foo.helper")
    _stub_audit(monkeypatch, tmp_path, _TASK_BODY, pr_body=f"Closes #7\n\n{block}")
    code = _audit()
    out = capsys.readouterr().out
    assert code == 1
    assert "submission: proof" in out
    assert "is not the task's target" in out


def test_a_pr_that_does_not_change_the_linked_target_file_gets_the_ordinary_contract(
    tmp_path: Path, monkeypatch, capsys  # type: ignore[no-untyped-def]
) -> None:
    # The PR touches one file; the linked task targets another. The link
    # does not describe this submission, so the placeholder the PR adds
    # is judged by count like any other.
    _stub_audit(
        monkeypatch,
        tmp_path,
        _TASK_BODY,
        pr_body=f"Closes #7\n\n{_BLOCK}",
        base={_OTHER_FILE: "theorem MyProj.Bar.other : True := trivial\n"},
        head={
            _OTHER_FILE: (
                "theorem MyProj.Bar.other : True := trivial\n"
                "theorem MyProj.Bar.step : True := by\n  sorry\n"
            )
        },
    )
    code = _audit()
    out = capsys.readouterr().out
    assert code == 1
    assert "submission: proof" in out
    assert "is not among the files this PR changes" in out
    assert f"{_OTHER_FILE}: net-new placeholders" in out


def test_a_second_changed_file_gets_the_ordinary_contract(
    tmp_path: Path, monkeypatch, capsys  # type: ignore[no-untyped-def]
) -> None:
    # The relaxation is scoped to the task's target file. A declared
    # obligation does not license a placeholder in another file the PR
    # happens to touch.
    _stub_audit(
        monkeypatch,
        tmp_path,
        _TASK_BODY,
        pr_body=f"Closes #7\n\n{_BLOCK}",
        base={_TARGET_FILE: _BASE_FILE, _OTHER_FILE: "-- nothing yet\n"},
        head={
            _TARGET_FILE: _HEAD_FILE,
            _OTHER_FILE: "theorem MyProj.Bar.step : True := by\n  sorry\n",
        },
    )
    code = _audit()
    out = capsys.readouterr().out
    assert code == 1
    assert "submission: reduction" in out
    assert f"{_TARGET_FILE}: clean" in out
    assert f"{_OTHER_FILE}: net-new placeholders" in out


def test_a_reduction_refusal_says_which_placeholders_the_table_holds(
    tmp_path: Path, monkeypatch, capsys  # type: ignore[no-untyped-def]
) -> None:
    # A finding's counts are the file's totals while its rows are only
    # the placeholders the reduction contract disallows — here two
    # placeholders at head against none at base, one of them a declared
    # obligation and so absent from the table. The report says so, or
    # the delta above a shorter table reads as a miscount.
    _stub_audit(
        monkeypatch,
        tmp_path,
        _TASK_BODY,
        pr_body=f"Closes #7\n\n{_BLOCK}",
        base={_TARGET_FILE: "-- nothing yet\n"},
        head={
            _TARGET_FILE: (
                "theorem MyProj.Foo.add_comm : True := by\n  sorry\n"
                "theorem MyProj.Foo.step : True := by\n  sorry\n"
            )
        },
    )
    code = _audit()
    out = capsys.readouterr().out
    assert code == 1
    assert "submission: reduction" in out
    assert "base 0 → head 2" in out
    assert "| `MyProj.Foo.add_comm` | 2 |" in out
    assert "lists only the placeholders that contract disallows" in out
