"""Tests for the claim-time deps advisory (`client.deps`)."""

from __future__ import annotations

from client import deps as deps_mod
from client import github as gh
from client.deps import find_open_deps, format_deps_report


class _FakeIssue:
    def __init__(self, state: str) -> None:
        self.state = state


def test_no_deps_returns_empty(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    assert find_open_deps("a/p", []) == []


def test_open_deps_detected(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    states = {3: "closed", 7: "open", 9: "open"}
    monkeypatch.setattr(
        deps_mod.gh, "get_issue", lambda r, n: _FakeIssue(states[n])
    )
    assert find_open_deps("a/p", [3, 7, 9]) == [7, 9]


def test_all_closed_returns_empty(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(deps_mod.gh, "get_issue", lambda r, n: _FakeIssue("closed"))
    assert find_open_deps("a/p", [1, 2]) == []


def test_fetch_failure_skipped_not_raised(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Advisory must not break the claim happy path: a dep we can't
    # fetch is silently skipped.
    def _get(r, n):  # type: ignore[no-untyped-def]
        if n == 5:
            raise gh.GitHubError("404")
        return _FakeIssue("open")

    monkeypatch.setattr(deps_mod.gh, "get_issue", _get)
    assert find_open_deps("a/p", [5, 6]) == [6]


def test_format_empty_when_no_open_deps() -> None:
    assert format_deps_report([]) == ""


def test_format_lists_refs() -> None:
    out = format_deps_report([7, 9])
    assert "#7" in out
    assert "#9" in out
    assert "⚠" in out
