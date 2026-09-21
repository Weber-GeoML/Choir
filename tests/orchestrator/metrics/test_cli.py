"""Tests for the metrics CLI."""

from __future__ import annotations

import io
import json
import subprocess
from dataclasses import dataclass

from gate.state.task_record import ProjectRef, TaskRecord, TaskType
from orchestrator.metrics import cli
from orchestrator.metrics.cli import _emit_csv
from orchestrator.tasks.serialize import task_to_body


@dataclass
class _FakeProc:
    stdout: str
    stderr: str = ""
    returncode: int = 0


def _payload_with_one_task() -> str:
    body = task_to_body(
        TaskRecord(
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
    )
    return json.dumps([{
        "number": 42,
        "title": "prove foo",
        "url": "https://x/42",
        "state": "closed",
        "labels": [{"name": "choir/task"}, {"name": "choir/type:prove"}],
        "body": body,
        "createdAt": "2026-05-01T12:00:00Z",
        "closedAt": "2026-05-01T13:30:00Z",
        "assignees": [{"login": "alice"}],
    }])


def test_cli_summarize_json(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: _FakeProc(stdout=_payload_with_one_task()),
    )
    code = cli.main(["summarize", "alice/proj", "--format", "json"])
    assert code == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert len(data) == 1
    assert data[0]["number"] == 42
    assert data[0]["contributor"] == "alice"
    assert data[0]["duration_seconds"] == 5400


def test_cli_summarize_csv(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: _FakeProc(stdout=_payload_with_one_task()),
    )
    code = cli.main(["summarize", "alice/proj", "--format", "csv"])
    assert code == 0
    out = capsys.readouterr().out
    lines = out.strip().splitlines()
    assert lines[0].startswith("number,title,url,state,task_type")
    assert "42" in lines[1]
    assert "alice" in lines[1]
    # Labels joined with ; in the CSV cell.
    assert "choir/task" in lines[1]


def test_cli_summarize_empty_repo(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: _FakeProc(stdout="[]"),
    )
    code = cli.main(["summarize", "alice/proj"])
    assert code == 0
    assert json.loads(capsys.readouterr().out) == []


def test_cli_summarize_propagates_gh_error_as_exit_2(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    def fake_run(cmd, **k):  # type: ignore[no-untyped-def]
        raise subprocess.CalledProcessError(1, cmd, stderr="rate limited")

    monkeypatch.setattr(subprocess, "run", fake_run)
    code = cli.main(["summarize", "alice/proj"])
    assert code == 2
    err = capsys.readouterr().err
    assert "rate limited" in err


def test_cli_csv_emits_header_even_with_zero_rows(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: _FakeProc(stdout="[]"),
    )
    buf = io.StringIO()
    _emit_csv([], buf)
    assert buf.getvalue().strip().startswith("number,")
