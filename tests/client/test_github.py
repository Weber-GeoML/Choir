"""Tests for `client.github.get_file_contents` (design note 13 §6).

Monkeypatches the module's private `_run` seam — the same subprocess
boundary every other function in this module funnels through — so no
real `gh` invocation happens.
"""

from __future__ import annotations

import base64

from client import github as gh
from gate.state.lease_arbiter import LEASE_COMMENTS_JQ


def test_decodes_successful_response(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    encoded = base64.b64encode(b"[project]\nchoir_protocol = 2\n").decode("ascii")
    calls: list[list[str]] = []

    def fake_run(cmd, *, check=True):  # type: ignore[no-untyped-def]
        calls.append(cmd)
        return f'{{"content": "{encoded}"}}'

    monkeypatch.setattr(gh, "_run", fake_run)
    out = gh.get_file_contents("acme/proofs", ".choir/project.toml")
    assert out == "[project]\nchoir_protocol = 2\n"
    assert calls == [["gh", "api", "repos/acme/proofs/contents/.choir/project.toml"]]


def test_ref_appends_query_param(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    encoded = base64.b64encode(b"x").decode("ascii")
    calls: list[list[str]] = []

    def fake_run(cmd, *, check=True):  # type: ignore[no-untyped-def]
        calls.append(cmd)
        return f'{{"content": "{encoded}"}}'

    monkeypatch.setattr(gh, "_run", fake_run)
    gh.get_file_contents("acme/proofs", ".choir/project.toml", ref="main")
    assert calls == [
        ["gh", "api", "repos/acme/proofs/contents/.choir/project.toml?ref=main"]
    ]


def test_github_error_returns_none(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def boom(cmd, *, check=True):  # type: ignore[no-untyped-def]
        raise gh.GitHubError("404 not found")

    monkeypatch.setattr(gh, "_run", boom)
    assert gh.get_file_contents("acme/proofs", ".choir/project.toml") is None


def test_malformed_json_returns_none(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(gh, "_run", lambda cmd, *, check=True: "not json")
    assert gh.get_file_contents("acme/proofs", ".choir/project.toml") is None


def test_missing_content_field_returns_none(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(gh, "_run", lambda cmd, *, check=True: '{"message": "Not Found"}')
    assert gh.get_file_contents("acme/proofs", ".choir/project.toml") is None


def test_bad_base64_returns_none(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        gh, "_run", lambda cmd, *, check=True: '{"content": "not-base64!!!"}'
    )
    assert gh.get_file_contents("acme/proofs", ".choir/project.toml") is None


# ---------------------------------------------------------------------------
# The comment wrappers spec D4's lease rides on
# ---------------------------------------------------------------------------


def test_list_issue_comments_paginates_and_asks_for_the_shared_projection(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    calls: list[list[str]] = []

    def fake_run(cmd, *, check=True):  # type: ignore[no-untyped-def]
        calls.append(cmd)
        return '[{"id": 1, "login": "alice", "body": "hi"}]'

    monkeypatch.setattr(gh, "_run", fake_run)
    out = gh.list_issue_comments("acme/proofs", 9)

    assert out == [{"id": 1, "login": "alice", "body": "hi"}]
    # `--paginate`: a truncated thread would hide the earliest claim, which
    # is the one that decides the lease. Both the flag and the `--jq` come
    # from `gate`, shared with the orchestrator's reader.
    assert calls == [
        [
            "gh",
            "api",
            "--paginate",
            "repos/acme/proofs/issues/9/comments",
            "--jq",
            LEASE_COMMENTS_JQ,
        ]
    ]


def test_list_issue_comments_tolerates_empty_output(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(gh, "_run", lambda cmd, *, check=True: "")
    assert gh.list_issue_comments("acme/proofs", 9) is None


def test_edit_comment_patches_the_comment_body(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: list[list[str]] = []
    monkeypatch.setattr(
        gh, "_run", lambda cmd, *, check=True: calls.append(cmd) or "{}"
    )

    gh.edit_comment("acme/proofs", 12345, "```choir-lease\nlogin: alice\n```")

    # `-f` (raw field), never `-F`: `-F` would read a leading `@` as a
    # filename and type-convert the value.
    assert calls == [
        [
            "gh",
            "api",
            "--method",
            "PATCH",
            "repos/acme/proofs/issues/comments/12345",
            "-f",
            "body=```choir-lease\nlogin: alice\n```",
        ]
    ]


def test_the_write_wrappers_that_need_push_access_are_gone() -> None:
    # Spec D4: a contributor has no repository write access, so these were
    # calls that either failed loudly (assignees) or, worse, silently did
    # nothing (labels). The orchestrator owns both now.
    for gone in (
        "add_label",
        "remove_label",
        "ensure_label_exists",
        "add_assignee",
        "remove_assignee",
        "get_assigned_events",
        "AssignedEvent",
    ):
        assert not hasattr(gh, gone), f"client.github still exposes {gone}"
