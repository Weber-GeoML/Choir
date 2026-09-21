"""Tests for the pure helpers in `client.submit`.

Subprocess-driven functions (push, gh pr create) are integration-tested
in the demo flow against a live repo.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from client import submit as submit_mod
from client._subprocess import CompletedRun, ToolNotFound
from client.submit import pr_body, pr_title
from client.workspace import LeaseMetadata


def _sample_meta(**overrides: object) -> LeaseMetadata:
    base = {
        "repo": "alice/proj",
        "issue": 42,
        "branch": "choir/42-add-comm",
        "pinned_commit": "1a2b3c4d",
        "claimed_at": "2026-05-10T15:32:00+00:00",
        "claimed_by": "alice",
        "target_file": "MyProj/Foo.lean",
        "target_decl": "MyProj.Foo.add_comm",
        "task_type": "prove",
    }
    base.update(overrides)
    return LeaseMetadata(**base)  # type: ignore[arg-type]


def test_pr_body_has_closes_keyword_for_auto_link() -> None:
    body = pr_body(_sample_meta())
    # GitHub's auto-close keywords: case-insensitive Close/Fix/Resolve.
    assert "Closes #42" in body


def test_pr_body_includes_claim_metadata() -> None:
    body = pr_body(_sample_meta())
    assert "@alice" in body
    assert "2026-05-10T15:32:00+00:00" in body
    assert "MyProj.Foo.add_comm" in body
    assert "MyProj/Foo.lean" in body


def test_pr_title_format() -> None:
    title = pr_title(_sample_meta())
    assert title == "choir(prove): MyProj.Foo.add_comm (closes #42)"


def test_pr_title_includes_task_type() -> None:
    title = pr_title(_sample_meta(task_type="golf"))
    assert "choir(golf):" in title


def test_pr_title_references_issue_number() -> None:
    title = pr_title(_sample_meta(issue=999))
    assert "(closes #999)" in title


# ---------------------------------------------------------------------------
# Bug #7 regression: _default_branch raises on failure (was: silently "main")
# ---------------------------------------------------------------------------


def _fake_run_factory(returncode: int, stdout: str, stderr: str = ""):  # type: ignore[no-untyped-def]
    def fake_run(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return CompletedRun(returncode=returncode, stdout=stdout, stderr=stderr)

    return fake_run


def test_default_branch_returns_value_on_success(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(submit_mod, "run", _fake_run_factory(0, "main\n"))
    assert submit_mod._default_branch("alice/repo") == "main"


def test_default_branch_returns_value_for_non_main_repo(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(submit_mod, "run", _fake_run_factory(0, "master\n"))
    assert submit_mod._default_branch("alice/repo") == "master"


def test_default_branch_raises_on_command_failure(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        submit_mod, "run", _fake_run_factory(1, "", "could not find repository\n")
    )
    with pytest.raises(submit_mod.SubmitError, match="could not detect default branch"):
        submit_mod._default_branch("alice/repo")


def test_default_branch_raises_on_empty_output(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(submit_mod, "run", _fake_run_factory(0, ""))
    with pytest.raises(submit_mod.SubmitError, match="empty response"):
        submit_mod._default_branch("alice/repo")


def test_default_branch_raises_when_gh_not_installed(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def fake_run(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise ToolNotFound("gh", "install from https://cli.github.com")

    monkeypatch.setattr(submit_mod, "run", fake_run)
    with pytest.raises(submit_mod.SubmitError, match="gh"):
        submit_mod._default_branch("alice/repo")


# ---------------------------------------------------------------------------
# Spec D4: the branch lives on the contributor's fork, never on the project
# repo, because a contributor has no write access there.
# ---------------------------------------------------------------------------


def test_the_fork_remote_never_touches_origin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The workspace is a worktree of the per-project store, so remotes live
    in the store's config. `origin`'s standard refspec is what keeps a store
    refresh from pruning in-flight `choir/*` branches (2026-06-20), so the
    fork remote must be additive and carry no fetch refspec of its own."""
    calls: list[list[str]] = []

    def _fake_run(args: list[str], **kwargs: object) -> object:
        calls.append(args)
        if args[:3] == ["git", "remote", "get-url"]:
            return CompletedRun(returncode=1, stdout="", stderr="no such remote")
        return CompletedRun(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(submit_mod, "run", _fake_run)
    submit_mod._ensure_fork_remote("octocat/proj", cwd=tmp_path)
    added = [c for c in calls if c[:3] == ["git", "remote", "add"]]
    assert added == [
        [
            "git", "remote", "add", "--no-tags", "choir-fork",
            "https://github.com/octocat/proj.git",
        ]
    ]
    assert not any("origin" in c for c in calls)


def test_a_stale_fork_url_is_corrected_rather_than_reused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A contributor who renamed or transferred their fork must not be left
    pushing at the old URL."""
    calls: list[list[str]] = []

    def _fake_run(args: list[str], **kwargs: object) -> object:
        calls.append(args)
        if args[:3] == ["git", "remote", "get-url"]:
            return CompletedRun(returncode=0, stdout="https://github.com/old/name.git\n", stderr="")
        return CompletedRun(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(submit_mod, "run", _fake_run)
    submit_mod._ensure_fork_remote("octocat/proj", cwd=tmp_path)
    assert [
        "git", "remote", "set-url", "choir-fork",
        "https://github.com/octocat/proj.git",
    ] in calls


def test_pr_lookup_uses_the_bare_branch_while_creation_uses_the_qualified_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two gh commands want different head spellings, and mixing them up
    fails silently rather than loudly.

    `gh pr create --head` needs `<owner>:<branch>` to open a cross-repo PR
    from the contributor's fork. `gh pr list --head` documents
    `"<owner>:<branch>" syntax not supported` and filters on `headRefName`,
    the bare name. Passing the qualified ref to the *lookup* matches
    nothing, so every re-run of submit concludes no PR exists and then dies
    creating a second one — submit's idempotency gone, with no error
    pointing at the cause.
    """
    seen: dict[str, list[str]] = {}

    def _fake_run(args: list[str], **kwargs: object) -> CompletedRun:
        if args[:3] == ["gh", "pr", "list"]:
            seen["list"] = args
            return CompletedRun(returncode=0, stdout="[]", stderr="")
        raise AssertionError(args)

    monkeypatch.setattr(submit_mod, "run", _fake_run)
    assert (
        submit_mod._find_open_pr("owner/proj", "choir/42-task", head_owner="octocat")
        is None
    )
    head_index = seen["list"].index("--head")
    assert seen["list"][head_index + 1] == "choir/42-task"
    assert ":" not in seen["list"][head_index + 1]


def test_pr_lookup_ignores_another_contributors_identically_named_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Branch names are derived from the issue number, so two contributors
    on one issue produce byte-identical `headRefName`s. `gh pr list --head`
    filters on that name alone and cannot distinguish the forks, so an
    unqualified lookup returns whichever PR came back first.

    If that is someone else's, submit reports success, comments "Submitted
    in <their PR>" on the issue, and never opens a PR for the work it just
    pushed — a silent loss of a finished proof.
    """
    other = {
        "url": "https://github.com/owner/proj/pull/7",
        "headRefName": "choir/42-task",
        "headRepositoryOwner": {"login": "someone-else"},
    }

    def _fake_run(args: list[str], **kwargs: object) -> CompletedRun:
        if args[:3] == ["gh", "pr", "list"]:
            return CompletedRun(returncode=0, stdout=json.dumps([other]), stderr="")
        raise AssertionError(args)

    monkeypatch.setattr(submit_mod, "run", _fake_run)
    assert (
        submit_mod._find_open_pr("owner/proj", "choir/42-task", head_owner="octocat")
        is None
    )


def test_pr_lookup_matches_our_own_fork_among_several(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reuse path still has to work: our own open PR must be found even
    when another fork's PR shares the branch name."""
    items = [
        {
            "url": "https://github.com/owner/proj/pull/7",
            "headRefName": "choir/42-task",
            "headRepositoryOwner": {"login": "someone-else"},
        },
        {
            "url": "https://github.com/owner/proj/pull/9",
            "headRefName": "choir/42-task",
            # Case differs: GitHub logins are case-insensitive, and a
            # case-sensitive compare here would open a duplicate PR.
            "headRepositoryOwner": {"login": "OctoCat"},
        },
    ]

    def _fake_run(args: list[str], **kwargs: object) -> CompletedRun:
        if args[:3] == ["gh", "pr", "list"]:
            return CompletedRun(returncode=0, stdout=json.dumps(items), stderr="")
        raise AssertionError(args)

    monkeypatch.setattr(submit_mod, "run", _fake_run)
    assert (
        submit_mod._find_open_pr("owner/proj", "choir/42-task", head_owner="octocat")
        == "https://github.com/owner/proj/pull/9"
    )
