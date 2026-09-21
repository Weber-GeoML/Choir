"""Tests for `client.fork` — which repository is my fork of this project?

The bug class these exist for: composing `<login>/<upstream-name>` produces a
name that is often right, and when it is wrong it resolves to a repository
that really exists. Nothing raises; the branch is pushed somewhere else.
"""

from __future__ import annotations

import json

import pytest

from client import fork as fork_mod
from client._subprocess import CompletedRun, ToolNotFound


def _graphql_reply(*names: str) -> str:
    nodes = [{"nameWithOwner": n} for n in names]
    return json.dumps({"data": {"repository": {"forks": {"nodes": nodes}}}})


def _is(args: list[str], *prefix: str) -> bool:
    return args[: len(prefix)] == list(prefix)


# ---------------------------------------------------------------------------
# Lookup, not composition.
# ---------------------------------------------------------------------------


def test_a_fork_that_github_renamed_is_found_under_its_real_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GitHub appends a suffix when the fork's name is already taken in the
    contributor's account, so forking `owner/proj` while they already own a
    `proj` yields `octocat/proj-1`. Composing the name would address
    `octocat/proj` — a repository that exists and is not the fork."""

    def _fake_run(args: list[str], **kwargs: object) -> CompletedRun:
        if _is(args, "gh", "api", "graphql"):
            return CompletedRun(
                returncode=0, stdout=_graphql_reply("octocat/proj-1"), stderr=""
            )
        raise AssertionError(args)

    monkeypatch.setattr(fork_mod, "run", _fake_run)
    assert fork_mod.find_fork("owner/proj") == "octocat/proj-1"


def test_a_fork_renamed_by_its_owner_is_found_under_the_new_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A contributor may rename their fork after creating it. The lookup
    reports the current name, which is what lets `_ensure_fork_remote`'s
    `set-url` self-heal instead of pushing at a URL that stopped
    resolving."""

    def _fake_run(args: list[str], **kwargs: object) -> CompletedRun:
        if _is(args, "gh", "api", "graphql"):
            return CompletedRun(
                returncode=0,
                stdout=_graphql_reply("octocat/my-choir-work"),
                stderr="",
            )
        raise AssertionError(args)

    monkeypatch.setattr(fork_mod, "run", _fake_run)
    assert fork_mod.find_fork("owner/proj") == "octocat/my-choir-work"


def test_no_fork_reads_as_none_rather_than_a_guess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_run(args: list[str], **kwargs: object) -> CompletedRun:
        return CompletedRun(returncode=0, stdout=_graphql_reply(), stderr="")

    monkeypatch.setattr(fork_mod, "run", _fake_run)
    assert fork_mod.find_fork("owner/proj") is None


def test_the_lookup_is_one_request_not_a_scan_of_every_fork(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`/repos/{o}/{r}/forks` paginates over every fork of the upstream —
    thousands of pages on a popular project to find one row. The GraphQL
    connection is restricted to forks the viewer owns, so the answer comes
    back in a single request."""
    calls: list[list[str]] = []

    def _fake_run(args: list[str], **kwargs: object) -> CompletedRun:
        calls.append(args)
        return CompletedRun(
            returncode=0, stdout=_graphql_reply("octocat/proj"), stderr=""
        )

    monkeypatch.setattr(fork_mod, "run", _fake_run)
    fork_mod.find_fork("owner/proj")
    assert len(calls) == 1
    assert not any("--paginate" in c for c in calls)
    joined = " ".join(calls[0])
    assert "ownerAffiliations" in joined


# ---------------------------------------------------------------------------
# Creation: only on a genuinely first task, and forking is asynchronous.
# ---------------------------------------------------------------------------


def test_an_existing_fork_is_reused_and_never_re_forked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A contributor working ten tasks on one project forks once. Reaching
    `gh repo fork` on every submit would be wasteful at best."""
    calls: list[list[str]] = []

    def _fake_run(args: list[str], **kwargs: object) -> CompletedRun:
        calls.append(args)
        if _is(args, "gh", "api", "graphql"):
            return CompletedRun(
                returncode=0, stdout=_graphql_reply("octocat/proj"), stderr=""
            )
        raise AssertionError(args)

    monkeypatch.setattr(fork_mod, "run", _fake_run)
    assert fork_mod.ensure_fork("owner/proj") == "octocat/proj"
    assert not any(_is(c, "gh", "repo", "fork") for c in calls)


def test_a_fork_that_appears_after_a_delay_is_waited_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Forking is asynchronous — the API accepts the request and copies the
    repository in the background, so an immediate push can hit a repo that
    is not ready yet."""
    lookups = {"n": 0}

    def _fake_run(args: list[str], **kwargs: object) -> CompletedRun:
        if _is(args, "gh", "api", "graphql"):
            lookups["n"] += 1
            found = lookups["n"] >= 3
            return CompletedRun(
                returncode=0,
                stdout=_graphql_reply(*(["octocat/proj"] if found else [])),
                stderr="",
            )
        if _is(args, "gh", "repo", "fork"):
            return CompletedRun(returncode=0, stdout="", stderr="")
        raise AssertionError(args)

    monkeypatch.setattr(fork_mod, "run", _fake_run)
    monkeypatch.setattr(fork_mod.time, "sleep", lambda _s: None)
    assert fork_mod.ensure_fork("owner/proj", poll_delay=0) == "octocat/proj"


def test_a_forks_exit_code_is_not_trusted_over_the_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`gh repo fork` is chatty on the already-exists path and its exit
    status has moved across versions. If the fork is there afterwards, the
    submit proceeds regardless of what that call returned."""

    def _fake_run(args: list[str], **kwargs: object) -> CompletedRun:
        if _is(args, "gh", "api", "graphql"):
            calls = getattr(_fake_run, "n", 0)
            _fake_run.n = calls + 1  # type: ignore[attr-defined]
            return CompletedRun(
                returncode=0,
                stdout=_graphql_reply(*(["octocat/proj"] if calls else [])),
                stderr="",
            )
        if _is(args, "gh", "repo", "fork"):
            return CompletedRun(returncode=1, stdout="", stderr="already exists")
        raise AssertionError(args)

    monkeypatch.setattr(fork_mod, "run", _fake_run)
    monkeypatch.setattr(fork_mod.time, "sleep", lambda _s: None)
    assert fork_mod.ensure_fork("owner/proj", poll_delay=0) == "octocat/proj"


def test_a_fork_that_never_appears_raises_rather_than_pushing_blind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_run(args: list[str], **kwargs: object) -> CompletedRun:
        if _is(args, "gh", "api", "graphql"):
            return CompletedRun(returncode=0, stdout=_graphql_reply(), stderr="")
        return CompletedRun(returncode=1, stdout="", stderr="nope")

    monkeypatch.setattr(fork_mod, "run", _fake_run)
    monkeypatch.setattr(fork_mod.time, "sleep", lambda _s: None)
    with pytest.raises(fork_mod.ForkError, match="did not appear"):
        fork_mod.ensure_fork("owner/proj", poll_attempts=2, poll_delay=0)


def test_a_missing_gh_is_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_run(args: list[str], **kwargs: object) -> CompletedRun:
        raise ToolNotFound("gh not found")

    monkeypatch.setattr(fork_mod, "run", _fake_run)
    with pytest.raises(fork_mod.ForkError, match="gh"):
        fork_mod.find_fork("owner/proj")


# ---------------------------------------------------------------------------
# The account that owns the upstream has no fork, and cannot have one.
# ---------------------------------------------------------------------------


def test_the_upstream_owner_is_recognized_so_submit_can_avoid_forking() -> None:
    """GitHub refuses to fork a repository into the account that owns it, so
    an overseer proving a task on their own project has no fork and never
    will — a supported shape since the 2026-07-09 self-review decision."""
    assert fork_mod.owns_upstream("octocat/proj", "octocat")


def test_owner_detection_is_case_insensitive() -> None:
    """GitHub logins are case-insensitive. A `gh` answer of `OctoCat` against
    an upstream spelled `octocat/…` must not read as a different account and
    send the owner down a fork path that cannot work."""
    assert fork_mod.owns_upstream("octocat/proj", "OctoCat")
    assert fork_mod.owns_upstream("OctoCat/proj", "octocat")


def test_a_contributor_is_not_mistaken_for_the_owner() -> None:
    assert not fork_mod.owns_upstream("owner/proj", "octocat")


def test_a_malformed_repo_is_rejected_rather_than_half_parsed() -> None:
    for bad in ("proj", "owner/", "/proj", "a/b/c"):
        with pytest.raises(fork_mod.ForkError):
            fork_mod.split_repo(bad)


# ---------------------------------------------------------------------------
# Fork sync is best-effort by design.
# ---------------------------------------------------------------------------


def test_sync_failure_is_reported_not_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fork whose default branch has diverged fails `gh repo sync`, which
    fast-forwards and refuses to clobber. That must never be the reason a
    finished proof cannot be submitted."""

    def _fake_run(args: list[str], **kwargs: object) -> CompletedRun:
        return CompletedRun(returncode=1, stdout="", stderr="would not fast-forward")

    monkeypatch.setattr(fork_mod, "run", _fake_run)
    assert fork_mod.sync_fork("octocat/proj") is False


def test_sync_never_passes_force(monkeypatch: pytest.MonkeyPatch) -> None:
    """`--force` hard-resets the destination branch. A contributor's own
    commits on their fork's default branch are theirs, not ours to discard."""
    calls: list[list[str]] = []

    def _fake_run(args: list[str], **kwargs: object) -> CompletedRun:
        calls.append(args)
        return CompletedRun(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(fork_mod, "run", _fake_run)
    fork_mod.sync_fork("octocat/proj")
    assert calls == [["gh", "repo", "sync", "octocat/proj"]]
