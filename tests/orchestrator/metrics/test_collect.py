"""Tests for `orchestrator.metrics.collect`.

Mocks the `gh` subprocess; integration against a real repo is left to
the demo runbook.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

import pytest

from gate.state.task_record import ProjectRef, TaskRecord, TaskType
from orchestrator.metrics.collect import (
    MetricsError,
    _duration_seconds,
    collect_task_metrics,
)
from orchestrator.tasks.serialize import task_to_body


@dataclass
class _FakeProc:
    stdout: str
    stderr: str = ""
    returncode: int = 0


def _task() -> TaskRecord:
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


# ---------------------------------------------------------------------------
# _duration_seconds
# ---------------------------------------------------------------------------


def test_duration_basic() -> None:
    s = _duration_seconds("2026-05-01T12:00:00Z", "2026-05-01T13:00:00Z")
    assert s == 3600


def test_duration_none_when_not_closed() -> None:
    assert _duration_seconds("2026-05-01T12:00:00Z", None) is None


def test_duration_handles_z_suffix_and_offsets() -> None:
    # Both Z-suffixed and offset-suffixed should parse.
    s = _duration_seconds("2026-05-01T12:00:00Z", "2026-05-01T12:30:00+00:00")
    assert s == 1800


def test_duration_returns_none_on_garbage() -> None:
    assert _duration_seconds("not-a-date", "also-not") is None


# ---------------------------------------------------------------------------
# collect_task_metrics
# ---------------------------------------------------------------------------


def test_collect_basic(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    body = task_to_body(_task())
    payload = json.dumps([
        {
            "number": 1,
            "title": "prove foo",
            "url": "https://x/1",
            "state": "OPEN",
            "labels": [
                {"name": "choir/task"},
                {"name": "choir/type:prove"},
                {"name": "choir/available"},
            ],
            "body": body,
            "createdAt": "2026-05-01T12:00:00Z",
            "closedAt": None,
            "assignees": [],
        }
    ])
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeProc(stdout=payload))
    out = collect_task_metrics("alice/proj")
    assert len(out) == 1
    m = out[0]
    assert m.number == 1
    assert m.state == "open"
    assert m.task_type == "prove"
    assert m.closed_at is None
    assert m.duration_seconds is None
    assert m.contributor is None
    assert "choir/task" in m.labels


def test_collect_closed_issue_has_duration_and_contributor(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    body = task_to_body(_task())
    payload = json.dumps([
        {
            "number": 2,
            "title": "prove bar",
            "url": "https://x/2",
            "state": "closed",
            "labels": [{"name": "choir/task"}, {"name": "choir/type:prove"}],
            "body": body,
            "createdAt": "2026-05-01T12:00:00Z",
            "closedAt": "2026-05-01T13:00:00Z",
            "assignees": [{"login": "alice"}],
        }
    ])
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeProc(stdout=payload))
    out = collect_task_metrics("alice/proj", state="closed")
    assert out[0].duration_seconds == 3600
    assert out[0].contributor == "alice"


def test_collect_falls_back_to_type_label_when_body_invalid(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Issue body doesn't parse, but the type-label is still there.
    # The metric should pick up `task_type` from the label.
    payload = json.dumps([
        {
            "number": 3,
            "title": "broken",
            "url": "https://x/3",
            "state": "open",
            "labels": [{"name": "choir/task"}, {"name": "choir/type:golf"}],
            "body": "totally garbage no frontmatter",
            "createdAt": "2026-05-01T12:00:00Z",
            "closedAt": None,
            "assignees": [],
        }
    ])
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeProc(stdout=payload))
    out = collect_task_metrics("alice/proj")
    assert out[0].task_type == "golf"


def test_collect_unknown_type_label_yields_none(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Misspelled type label shouldn't be silently accepted as a valid type.
    payload = json.dumps([
        {
            "number": 4,
            "title": "weird",
            "url": "https://x/4",
            "state": "open",
            "labels": [{"name": "choir/task"}, {"name": "choir/type:nonsense"}],
            "body": "garbage",
            "createdAt": "2026-05-01T12:00:00Z",
            "closedAt": None,
            "assignees": [],
        }
    ])
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeProc(stdout=payload))
    out = collect_task_metrics("alice/proj")
    assert out[0].task_type is None


def test_collect_label_filter_propagated_to_gh(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    captured = []

    def fake_run(cmd, **k):  # type: ignore[no-untyped-def]
        captured.append(cmd)
        return _FakeProc(stdout="[]")

    monkeypatch.setattr(subprocess, "run", fake_run)
    collect_task_metrics("alice/proj", label="benchmark/quals2026")
    cmd = captured[0]
    label_args = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--label"]
    assert "choir/task" in label_args
    assert "benchmark/quals2026" in label_args


def test_collect_raises_on_gh_error(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def fake_run(cmd, **k):  # type: ignore[no-untyped-def]
        raise subprocess.CalledProcessError(1, cmd, stderr="not authenticated")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(MetricsError, match="not authenticated"):
        collect_task_metrics("alice/proj")


def test_task_metric_as_dict_lists_labels(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    body = task_to_body(_task())
    payload = json.dumps([
        {
            "number": 5,
            "title": "t",
            "url": "https://x/5",
            "state": "open",
            "labels": [{"name": "choir/task"}, {"name": "choir/type:prove"}],
            "body": body,
            "createdAt": "2026-05-01T12:00:00Z",
            "closedAt": None,
            "assignees": [],
        }
    ])
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeProc(stdout=payload))
    m = collect_task_metrics("alice/proj")[0]
    d = m.as_dict()
    assert isinstance(d["labels"], list)
    assert "choir/task" in d["labels"]
