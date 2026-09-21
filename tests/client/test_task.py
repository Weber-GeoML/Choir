"""`prepare_task` — what a claim leaves behind before the backend runs.

The stores in `repo_store`, `mathlib_cache` and `build_store` were
written, tested and documented, and nothing on this path called them:
`prepare_workspace_build_cache` was reachable only from its own test. So
every workspace fetched its own dependency set — measured at ~7.4 GB
apiece, three workers paying it separately. A unit test of a function
nobody calls cannot catch that; this asserts the call happens.
"""

from __future__ import annotations

import pytest

from client import task as task_mod
from client.build_store import format_cache_report
from gate.state.intake import ParseSuccess
from gate.state.task_record import ProjectRef, TaskRecord, TaskType


def test_a_claim_fills_the_workspace_from_the_stores(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Before the backend runs, or the download has already happened."""
    calls: list[str] = []

    monkeypatch.setattr(
        task_mod, "prepare_workspace_build_cache",
        lambda p: calls.append(str(p)) or {"deps": "hit", "build_restore": "hit"},
    )
    monkeypatch.setattr(task_mod, "setup_workspace", lambda **kw: tmp_path)
    monkeypatch.setattr(task_mod, "heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(task_mod, "check_pins", lambda p: [])
    monkeypatch.setattr(task_mod, "format_pin_report", lambda r: "")
    monkeypatch.setattr(task_mod, "workspace_profile", lambda p: _profile())
    monkeypatch.setattr(task_mod, "find_open_deps", lambda r, d: [])
    monkeypatch.setattr(task_mod, "format_deps_report", lambda d: "")
    monkeypatch.setattr(task_mod.gh, "current_user", lambda: "alice")
    monkeypatch.setattr(task_mod.gh, "get_issue", lambda r, n: _issue())
    monkeypatch.setattr(task_mod, "parse_issue_body", lambda b, expected_repo: _parsed())

    prepared = task_mod.prepare_task("owner/proj", 7)
    assert calls == [str(tmp_path)], "the stores were not consulted"
    assert any("no download" in a for a in prepared.advisories), (
        "what the stores did must reach the claimer — reported silently, "
        "nobody notices the store is being missed"
    )


@pytest.mark.parametrize(
    "deps,expect",
    [
        ("hit", "no download, no copy"),
        ("fetched", "moved into the store"),
        ("fetched-nostore", "could not be written"),
        ("skipped", "the build will fetch them"),
        ("present", "already in this workspace"),
        ("no-manifest", "no dependency manifest"),
        ("something-new", ""),
    ],
)
def test_every_outcome_says_what_it_means(deps: str, expect: str) -> None:
    """Including the unknown one, which must stay quiet rather than lie."""
    note = format_cache_report({"deps": deps, "build_restore": "miss"})
    assert expect in note
    if not expect:
        assert note == ""


def _profile():  # type: ignore[no-untyped-def]
    class _P:
        search_tooling_note = ""
    return _P()


def _issue():  # type: ignore[no-untyped-def]
    class _I:
        body = "irrelevant; the parse is stubbed"
    return _I()


def _parsed():  # type: ignore[no-untyped-def]
    record = TaskRecord(
        **{"choir-task-version": 1},
        type=TaskType.PROVE,
        target_file="Proj/Core.lean",
        target_decl="Proj.main",
        project_ref=ProjectRef(
            repo="owner/proj", commit="a" * 40, toolchain="leanprover/lean4:v4.33.0"
        ),
        deps=[],
    )
    return ParseSuccess(record=record, body_prose="prose")
