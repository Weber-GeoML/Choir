"""Tests for the comment-edit heartbeat in `client.heartbeat` (spec D4).

The label-rolling helper these used to cover (`heartbeat_operations`) is
gone: a contributor cannot write labels without repository access. What
matters now is that a long lease costs the thread one comment, that the
comment being edited is never the claim comment, and that a non-holder
writes nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from client import github as gh
from client import heartbeat as hb
from client.heartbeat import heartbeat, heartbeat_comment_body, heartbeat_target
from client.workspace import LeaseMetadata
from gate.protocol import PROTOCOL_VERSION
from gate.state.lease_arbiter import (
    LeaseComment,
    decide_lease,
    lease_comments_from_api,
)
from gate.state.lease_comment import (
    ACTION_CLAIM,
    ACTION_HEARTBEAT,
    parse_lease_comment,
)

NOW = datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC)


def _stamp(offset_hours: float = 0.0) -> str:
    return (NOW + timedelta(hours=offset_hours)).isoformat().replace("+00:00", "Z")


def _comment(cid: int, login: str, action: str) -> LeaseComment:
    return LeaseComment(id=cid, login=login, action=action, updated_at=_stamp())


# ---------------------------------------------------------------------------
# heartbeat_target — which comment a beat edits
# ---------------------------------------------------------------------------


def test_no_prior_heartbeat_means_post_a_new_one() -> None:
    comments = [_comment(1, "alice", ACTION_CLAIM)]
    assert heartbeat_target(comments, "alice") is None


def test_the_claim_comment_is_never_the_edit_target() -> None:
    # Editing the claim would change the action `decide_lease` establishes
    # the holder from — the beat would destroy the lease it means to keep.
    comments = [_comment(1, "alice", ACTION_CLAIM)]
    assert heartbeat_target(comments, "alice") is None


def test_an_existing_heartbeat_is_the_edit_target() -> None:
    comments = [_comment(1, "alice", ACTION_CLAIM), _comment(2, "alice", ACTION_HEARTBEAT)]
    assert heartbeat_target(comments, "alice") == 2


def test_someone_elses_heartbeat_is_not_our_target() -> None:
    comments = [
        _comment(1, "alice", ACTION_CLAIM),
        _comment(2, "bob", ACTION_HEARTBEAT),
    ]
    assert heartbeat_target(comments, "alice") is None


def test_the_latest_of_our_own_heartbeats_wins() -> None:
    # Shouldn't happen (one beat comment per lease), but if a duplicate
    # exists the highest id is the live one — `decide_lease` reads freshness
    # off the id-highest comment.
    comments = [
        _comment(5, "alice", ACTION_HEARTBEAT),
        _comment(9, "alice", ACTION_HEARTBEAT),
        _comment(7, "alice", ACTION_HEARTBEAT),
    ]
    assert heartbeat_target(comments, "alice") == 9


def test_a_re_claim_above_our_heartbeat_forces_a_new_beat_comment() -> None:
    """The bug this guards, checked against `decide_lease` rather than guessed.

    `decide_lease` records `last_signal[login]` while walking the thread in
    id order, so a login's freshness comes from its id-highest comment only.
    A worker that claimed, heartbeat, crashed, re-claimed (a higher-id claim
    comment) and then went on editing its old heartbeat would age out of its
    own lease while beating on schedule. So when our id-highest comment is
    not a heartbeat, we post a new one instead of editing behind it.
    """
    comments = [
        _comment(1, "alice", ACTION_CLAIM),
        _comment(2, "alice", ACTION_HEARTBEAT),
        _comment(3, "alice", ACTION_CLAIM),  # the re-claim
    ]
    assert heartbeat_target(comments, "alice") is None


def test_the_edited_beat_is_the_comment_decide_lease_actually_reads() -> None:
    # End-to-end against the real arbiter: refresh what `heartbeat_target`
    # picks and the lease must read live.
    rows = _rows((1, "alice", "claim"), (2, "alice", "heartbeat"))
    rows[0]["updated_at"] = _stamp(-100)  # the claim is ancient
    comments = lease_comments_from_api(rows)

    target = heartbeat_target(comments, "alice")
    assert target == 2
    refreshed = [
        LeaseComment(
            id=c.id,
            login=c.login,
            action=c.action,
            updated_at=_stamp() if c.id == target else c.updated_at,
        )
        for c in comments
    ]
    assert (
        decide_lease(refreshed, stale_after_hours=24, now=NOW).holder == "alice"
    )


# ---------------------------------------------------------------------------
# heartbeat_comment_body
# ---------------------------------------------------------------------------


def test_the_body_parses_as_a_heartbeat_for_us() -> None:
    claim = parse_lease_comment(heartbeat_comment_body("alice", NOW))
    assert claim is not None
    assert claim.login == "alice"
    assert claim.action == ACTION_HEARTBEAT
    assert claim.protocol == PROTOCOL_VERSION


def test_two_beats_at_different_times_differ() -> None:
    # Load-bearing: the lease block alone is byte-identical every beat, and
    # an unchanged body is exactly where GitHub might leave `updated_at`
    # alone — which would silently starve a live lease.
    first = heartbeat_comment_body("alice", NOW)
    second = heartbeat_comment_body("alice", NOW + timedelta(minutes=5))
    assert first != second


def test_the_timestamp_lives_outside_the_fenced_block() -> None:
    body = heartbeat_comment_body("alice", NOW)
    block_end = body.index("```", body.index("```") + 3)
    assert "Lease refreshed" in body[block_end:]
    assert "Lease refreshed" not in body[:block_end]


# ---------------------------------------------------------------------------
# heartbeat — the GitHub round trip
# ---------------------------------------------------------------------------


def _rows(*specs: tuple[int, str, str]) -> list[dict[str, Any]]:
    return [
        {
            "id": cid,
            "login": login,
            "body": f"```choir-lease\nlogin: {login}\naction: {action}\n"
            f"protocol: {PROTOCOL_VERSION}\n```\n",
            "updated_at": _stamp(),
        }
        for cid, login, action in specs
    ]


class _Recorder:
    def __init__(self) -> None:
        self.posted: list[str] = []
        self.edited: list[tuple[int, str]] = []

    def post(self, repo: str, number: int, body: str) -> None:
        self.posted.append(body)

    def edit(self, repo: str, comment_id: int, body: str) -> None:
        self.edited.append((comment_id, body))


def _wire(
    monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, Any]]
) -> _Recorder:
    rec = _Recorder()
    monkeypatch.setattr(gh, "current_user", lambda: "alice")
    monkeypatch.setattr(gh, "list_issue_comments", lambda r, n: rows)
    monkeypatch.setattr(gh, "post_comment", rec.post)
    monkeypatch.setattr(gh, "edit_comment", rec.edit)
    return rec


def test_the_first_beat_posts_a_heartbeat_comment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = _wire(monkeypatch, _rows((1, "alice", "claim")))

    assert heartbeat("alice/proj", 1, now=NOW) is True
    assert len(rec.posted) == 1
    assert rec.edited == []


def test_later_beats_edit_that_comment_instead_of_posting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = _wire(
        monkeypatch, _rows((1, "alice", "claim"), (2, "alice", "heartbeat"))
    )

    assert heartbeat("alice/proj", 1, now=NOW) is True
    assert rec.posted == []
    assert [cid for cid, _body in rec.edited] == [2]


def test_a_non_holder_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    # bob claimed first, so bob holds it. Our beats would be ignored by
    # every reader; don't write them.
    rec = _wire(monkeypatch, _rows((1, "bob", "claim"), (2, "alice", "claim")))

    assert heartbeat("alice/proj", 1, now=NOW) is False
    assert rec.posted == []
    assert rec.edited == []


def test_no_lease_at_all_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _wire(monkeypatch, [])

    assert heartbeat("alice/proj", 1, now=NOW) is False
    assert rec.posted == []
    assert rec.edited == []


def test_a_released_lease_is_not_heartbeaten(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = _wire(
        monkeypatch, _rows((1, "alice", "claim"), (2, "alice", "release"))
    )

    assert heartbeat("alice/proj", 1, now=NOW) is False
    assert rec.posted == []
    assert rec.edited == []


def test_an_unreadable_thread_returns_false_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire(monkeypatch, _rows((1, "alice", "claim")))

    def boom(repo: str, number: int) -> object:
        raise gh.GitHubError("rate limited")

    monkeypatch.setattr(gh, "list_issue_comments", boom)
    assert heartbeat("alice/proj", 1, now=NOW) is False


def test_a_refused_edit_returns_false_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Heartbeats are advisory: a GitHub failure must not stop the work.
    _wire(monkeypatch, _rows((1, "alice", "claim"), (2, "alice", "heartbeat")))

    def boom(repo: str, comment_id: int, body: str) -> None:
        raise gh.GitHubError("403")

    monkeypatch.setattr(gh, "edit_comment", boom)
    assert heartbeat("alice/proj", 1, now=NOW) is False


def test_a_stale_holder_is_not_us_so_we_do_not_beat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Our own lease went stale and passed to the next live claimant. Beating
    # now would be writing to a lease we no longer hold.
    rows = _rows((1, "alice", "claim"), (2, "bob", "claim"))
    rows[0]["updated_at"] = _stamp(-72)
    rec = _wire(monkeypatch, rows)

    assert heartbeat("alice/proj", 1, now=NOW, stale_after_hours=24) is False
    assert rec.posted == []
    assert rec.edited == []


def test_login_can_be_supplied_to_skip_the_user_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire(monkeypatch, _rows((1, "alice", "claim")))

    def fail() -> str:
        raise AssertionError("current_user must not be called when login is given")

    monkeypatch.setattr(gh, "current_user", fail)
    assert heartbeat("alice/proj", 1, login="alice", now=NOW) is True


def test_module_reads_the_thread_through_the_shared_client_reader() -> None:
    # Not a behavioural assertion so much as a structural one: the heartbeat
    # must see exactly what `claim` sees, or the two could disagree about
    # who holds the lease.
    assert hb.read_lease_comments.__module__ == "client.lease"


# --- session resolution (the CLI's beat) -----------------------------------
#
# A lease is claimed and beaten by separate invocations, so the session the
# claim was made under has to be read back from the workspace. `heartbeat`
# keys the holder check on the `(login, session)` pair, so a beat that
# defaults the session to `""` against a real claim writes nothing.

SESSION = "0123456789abcdef"


def _rows_with_session(*specs: tuple[int, str, str, str]) -> list[dict[str, Any]]:
    return [
        {
            "id": cid,
            "login": login,
            "body": f"```choir-lease\nlogin: {login}\naction: {action}\n"
            f"session: {session}\nprotocol: {PROTOCOL_VERSION}\n```\n",
            "updated_at": _stamp(),
        }
        for cid, login, action, session in specs
    ]


def _lease_file(path: Path, repo: str, issue: int, session: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    LeaseMetadata(
        repo=repo,
        issue=issue,
        branch=f"choir/{issue}-x",
        pinned_commit="a" * 40,
        claimed_at=_stamp(),
        claimed_by="alice",
        target_file="Proj/X.lean",
        target_decl="Proj.x",
        task_type="prove",
        session=session,
    ).write(path / ".choir-lease.json")


def test_a_beat_on_a_session_claim_needs_that_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = _wire(monkeypatch, _rows_with_session((1, "alice", "claim", SESSION)))

    assert heartbeat("alice/proj", 1, now=NOW) is False
    assert rec.posted == [] and rec.edited == []

    assert heartbeat("alice/proj", 1, session=SESSION, now=NOW) is True
    assert len(rec.posted) == 1


def test_heartbeat_for_issue_reads_the_session_from_the_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CHOIR_WORK_ROOT", str(tmp_path))
    _lease_file(tmp_path / "alice/proj" / "1", "alice/proj", 1, SESSION)
    rec = _wire(monkeypatch, _rows_with_session((1, "alice", "claim", SESSION)))

    written, session = hb.heartbeat_for_issue("alice/proj", 1, now=NOW)

    assert written is True
    assert session == SESSION
    assert len(rec.posted) == 1


def test_an_explicit_session_overrides_the_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CHOIR_WORK_ROOT", str(tmp_path))
    rec = _wire(monkeypatch, _rows_with_session((1, "alice", "claim", SESSION)))

    written, session = hb.heartbeat_for_issue(
        "alice/proj", 1, session=SESSION, now=NOW
    )

    assert written is True
    assert session == SESSION
    assert len(rec.posted) == 1


def test_no_workspace_reports_an_unknown_session_rather_than_a_lost_lease(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CHOIR_WORK_ROOT", str(tmp_path))
    rec = _wire(monkeypatch, _rows_with_session((1, "alice", "claim", SESSION)))

    written, session = hb.heartbeat_for_issue("alice/proj", 1, now=NOW)

    assert written is False
    # `None`, not `""`: nothing was written because the session is unknown,
    # which is a different diagnosis from holding no lease.
    assert session is None
    assert rec.posted == [] and rec.edited == []


def test_a_sessionless_lease_still_beats(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CHOIR_WORK_ROOT", str(tmp_path))
    _lease_file(tmp_path / "alice/proj" / "1", "alice/proj", 1, "")
    rec = _wire(monkeypatch, _rows((1, "alice", "claim")))

    written, session = hb.heartbeat_for_issue("alice/proj", 1, now=NOW)

    assert written is True
    assert session == ""
    assert len(rec.posted) == 1


def test_a_workspace_for_another_lease_is_not_used(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CHOIR_WORK_ROOT", str(tmp_path))
    _lease_file(tmp_path / "alice/proj" / "1", "alice/proj", 2, SESSION)
    _wire(monkeypatch, _rows_with_session((1, "alice", "claim", SESSION)))

    assert hb.session_for_lease("alice/proj", 1) is None
