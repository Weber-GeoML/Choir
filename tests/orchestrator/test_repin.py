from __future__ import annotations

import pytest

from gate.state.task_record import ProjectRef, TaskRecord, TaskType
from orchestrator import repin
from orchestrator.repin import repin_available
from orchestrator.tasks.api import MaintainerError, TaskHandle

NEW = "b" * 40
OLD = "a" * 40


def _handle(number: int, target_file: str, commit: str = OLD) -> TaskHandle:
    return TaskHandle(
        number=number,
        title=f"task {number}",
        url=f"https://example.invalid/{number}",
        state="open",
        labels=("choir/task", "choir/available"),
        record=TaskRecord(
            **{
                "choir-task-version": 1,
                "type": TaskType.PROVE,
                "target_file": target_file,
                "target_decl": "a",
                "project_ref": ProjectRef(
                    repo="o/p", commit=commit, toolchain="leanprover/lean4:v4.27.0"
                ),
                "deps": [],
            }
        ),
    )


@pytest.fixture
def calls(monkeypatch):
    """Record the pins written, and what was asked of GitHub."""
    written: list[tuple[int, str]] = []
    listed: dict[str, object] = {}

    def _list(repo, **kw):
        listed.update(kw)
        return _list.handles

    _list.handles = []
    monkeypatch.setattr(repin, "list_choir_tasks", _list)
    monkeypatch.setattr(
        repin, "set_task_pin", lambda r, n, c: written.append((n, c))
    )
    return {"written": written, "listed": listed, "handles": _list}


def test_a_task_on_a_touched_file_moves_to_the_merge_commit(calls) -> None:
    calls["handles"].handles = [_handle(1, "A.lean")]
    result = repin_available("o/p", NEW, ["A.lean"])
    assert calls["written"] == [(1, NEW)]
    assert [e["issue"] for e in result["repinned"]] == [1]


def test_a_task_on_an_untouched_file_is_left_alone(calls) -> None:
    calls["handles"].handles = [_handle(1, "A.lean")]
    assert repin_available("o/p", NEW, ["B.lean"])["repinned"] == []
    assert calls["written"] == []


def test_only_unclaimed_tasks_are_considered(calls) -> None:
    """The whole policy: a claimed task keeps the pin its worker claimed."""
    calls["handles"].handles = [_handle(1, "A.lean")]
    repin_available("o/p", NEW, ["A.lean"])
    assert calls["listed"]["label"] == "choir/available"
    assert calls["listed"]["state"] == "open"


def test_a_task_already_at_the_commit_is_not_rewritten(calls) -> None:
    calls["handles"].handles = [_handle(1, "A.lean", commit=NEW)]
    assert repin_available("o/p", NEW, ["A.lean"])["repinned"] == []
    assert calls["written"] == []


def test_one_github_failure_does_not_abandon_the_rest(calls, monkeypatch) -> None:
    calls["handles"].handles = [_handle(1, "A.lean"), _handle(2, "A.lean")]

    def _flaky(repo, number, commit):
        if number == 1:
            raise MaintainerError("gh exploded")
        calls["written"].append((number, commit))

    monkeypatch.setattr(repin, "set_task_pin", _flaky)
    result = repin_available("o/p", NEW, ["A.lean"])
    assert [e["issue"] for e in result["failed"]] == [1]
    assert [e["issue"] for e in result["repinned"]] == [2]
    assert calls["written"] == [(2, NEW)]


def test_an_unparseable_issue_is_skipped(calls) -> None:
    calls["handles"].handles = [
        TaskHandle(number=9, title="broken", url="u", state="open",
                   labels=(), record=None)
    ]
    assert repin_available("o/p", NEW, ["A.lean"])["repinned"] == []
