"""Tests for `orchestrator.tasks.api`.

These mock the `gh` subprocess via monkeypatching `subprocess.run` on
the api module. End-to-end correctness against a real GitHub repo is
exercised through the demo runbook.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

import pytest

from gate.state.task_record import ProjectRef, TaskRecord, TaskType
from orchestrator.tasks.api import (
    LABEL_AVAILABLE,
    LABEL_TASK,
    MaintainerError,
    create_task_issue,
    get_task,
    list_choir_tasks,
    set_task_pin,
)
from orchestrator.tasks.serialize import body_to_task, task_to_body


def _record() -> TaskRecord:
    return TaskRecord(
        choir_task_version=1,
        type=TaskType.PROVE,
        target_file="Sample/Foo.lean",
        target_decl="Sample.foo",
        project_ref=ProjectRef(
            repo="alice/proj",
            commit="abcdef1234567",
            toolchain="leanprover/lean4:v4.5.0",
        ),
        deps=[],
        blueprint_ref=None,
    )


@dataclass
class _FakeProc:
    stdout: str
    stderr: str = ""
    returncode: int = 0


# ---------------------------------------------------------------------------
# create_task_issue
# ---------------------------------------------------------------------------


def test_create_task_issue_parses_returned_number(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls = []

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(cmd)
        # `gh issue create` prints the new issue URL.
        return _FakeProc(stdout="https://github.com/alice/proj/issues/42\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    n = create_task_issue(
        "alice/proj",
        task=_record(),
        title="prove foo",
    )
    assert n == 42
    # Verify the labels were passed correctly.
    cmd = calls[0]
    assert "--label" in cmd
    label_args = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--label"]
    assert LABEL_TASK in label_args
    assert LABEL_AVAILABLE in label_args


def test_create_task_issue_includes_extra_labels(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    captured = []

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        captured.append(cmd)
        return _FakeProc(stdout="https://github.com/alice/proj/issues/7\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    create_task_issue(
        "alice/proj",
        task=_record(),
        title="bench task",
        extra_labels=("benchmark/foo", "difficulty/hard"),
    )
    label_args = [
        captured[0][i + 1] for i, a in enumerate(captured[0]) if a == "--label"
    ]
    assert "benchmark/foo" in label_args
    assert "difficulty/hard" in label_args


def test_create_task_issue_body_roundtrips(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Critical: the body passed to `gh issue create` must be a valid
    # Choir issue body (intake-parsable). Capture and verify.
    captured = {}

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        # Body is the value following `--body`.
        idx = cmd.index("--body")
        captured["body"] = cmd[idx + 1]
        return _FakeProc(stdout="https://github.com/alice/proj/issues/1\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    create_task_issue(
        "alice/proj",
        task=_record(),
        title="prove foo",
        prose="describe me",
    )
    # Round-trip the captured body through intake.
    expected = task_to_body(_record(), "describe me")
    assert captured["body"] == expected


def test_create_task_issue_propagates_gh_error(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        raise subprocess.CalledProcessError(1, cmd, stderr="auth required")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(MaintainerError, match="auth required"):
        create_task_issue("alice/proj", task=_record(), title="x")


def test_create_task_issue_handles_gh_missing(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        raise FileNotFoundError("gh")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(MaintainerError, match=r"gh.*CLI not found"):
        create_task_issue("alice/proj", task=_record(), title="x")


# ---------------------------------------------------------------------------
# list_choir_tasks
# ---------------------------------------------------------------------------


def test_list_returns_handles_with_parsed_records(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    body = task_to_body(_record(), "p")
    payload = json.dumps([
        {
            "number": 1, "title": "t1", "url": "https://x/1",
            "state": "OPEN",
            "labels": [{"name": "choir/task"}, {"name": "choir/type:prove"}],
            "body": body,
        }
    ])
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeProc(stdout=payload))
    out = list_choir_tasks("alice/proj")
    assert len(out) == 1
    assert out[0].number == 1
    assert out[0].state == "open"
    assert out[0].record is not None
    assert "choir/task" in out[0].labels


def test_list_returns_none_record_for_malformed_body(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Issues with unparseable bodies are still returned (number + title +
    # url) so maintainer tooling can surface broken issues for repair.
    payload = json.dumps([
        {
            "number": 99, "title": "broken", "url": "https://x/99",
            "state": "open", "labels": [{"name": "choir/task"}],
            "body": "not a yaml front-matter at all",
        }
    ])
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeProc(stdout=payload))
    out = list_choir_tasks("alice/proj")
    assert out[0].record is None
    assert out[0].number == 99


def test_list_passes_task_type_label_filter(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    captured = []

    def fake_run(cmd, **k):  # type: ignore[no-untyped-def]
        captured.append(cmd)
        return _FakeProc(stdout="[]")

    monkeypatch.setattr(subprocess, "run", fake_run)
    list_choir_tasks("alice/proj", task_type=TaskType.PROVE, label="benchmark/foo")
    cmd = captured[0]
    label_args = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--label"]
    assert "choir/type:prove" in label_args
    assert "benchmark/foo" in label_args


def test_list_filters_by_golf_type_label(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Pinned because the golf playbook relies on it (spec 2026-07-18).
    captured = []

    def fake_run(cmd, **k):  # type: ignore[no-untyped-def]
        captured.append(cmd)
        return _FakeProc(stdout="[]")

    monkeypatch.setattr(subprocess, "run", fake_run)
    list_choir_tasks("alice/proj", task_type=TaskType.GOLF)
    cmd = captured[0]
    label_args = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--label"]
    assert "choir/type:golf" in label_args


def test_list_default_state_is_all(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    captured = []
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **k: captured.append(cmd) or _FakeProc(stdout="[]"),
    )
    list_choir_tasks("alice/proj")
    cmd = captured[0]
    state_idx = cmd.index("--state")
    assert cmd[state_idx + 1] == "all"


# ---------------------------------------------------------------------------
# get_task
# ---------------------------------------------------------------------------


def test_get_task_returns_view(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    body = task_to_body(_record(), "describe me")
    payload = json.dumps({
        "number": 7, "title": "prove foo", "url": "https://x/7",
        "state": "open", "labels": [{"name": "choir/task"}],
        "body": body,
    })
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeProc(stdout=payload))
    view = get_task("alice/proj", 7)
    assert view.handle.number == 7
    assert view.record.target_decl == "Sample.foo"
    assert "describe me" in view.prose


def test_get_task_raises_on_malformed_body(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    payload = json.dumps({
        "number": 7, "title": "bad", "url": "https://x/7",
        "state": "open", "labels": [],
        "body": "garbage",
    })
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeProc(stdout=payload))
    with pytest.raises(MaintainerError, match="failed intake parse"):
        get_task("alice/proj", 7)


# ---------------------------------------------------------------------------
# set_task_pin
# ---------------------------------------------------------------------------


def test_set_task_pin_moves_the_commit_and_keeps_the_prose(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    prose = "Prove foo.\n\nSee `Sample.bar` for the shape."
    view = json.dumps({"number": 7, "title": "t", "url": "u", "state": "OPEN",
                       "labels": [], "body": task_to_body(_record(), prose)})
    sent: list[list[str]] = []

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        sent.append(cmd)
        return _FakeProc(stdout=view)

    monkeypatch.setattr(subprocess, "run", fake_run)
    set_task_pin("alice/proj", 7, "f" * 40)

    edit = sent[-1]
    assert edit[1:4] == ["issue", "edit", "7"]
    body = edit[edit.index("--body") + 1]
    moved, kept = body_to_task(body)
    assert moved.project_ref.commit == "f" * 40
    assert kept.strip() == prose
    assert moved.target_decl == _record().target_decl
