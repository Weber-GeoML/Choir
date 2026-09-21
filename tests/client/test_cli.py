"""Tests for `client.cli`'s claim "Next steps" hint, `choir update`, and the
interactive protocol-gate prompt in `cmd_claim` (design note 13 §6).

Whole-branch review finding D: the hint printed by `choir claim` after a
successful claim used to hardcode `run lake build` (a lean4-ism, now
routed through the profile's build command — separately fixed). Split
into a small helper (`_print_claim_next_steps`) so it's unit-testable
without mocking the full `gh`/workspace claim pipeline.
"""

from __future__ import annotations

import json
import sys
from argparse import Namespace
from pathlib import Path

import pytest

from client import cli as cli_mod
from client.cli import _print_claim_next_steps
from client.lease import ClaimOutcome, ClaimResult
from client.update import UpdateError, UpdateResult
from gate.provers.lean4 import LEAN4
from gate.provers.rocq import ROCQ
from gate.state.task_record import TaskRecord

_PROTOCOL_REASON = (
    "project requires protocol 3; this client speaks 2 — run 'choir update'"
)

_PROJECT_REF = {
    "repo": "owner/name",
    "commit": "abcdef1",
    "toolchain": "leanprover/lean4:v4.9.0",
}


def _prove_record() -> TaskRecord:
    return TaskRecord.model_validate(
        {
            "choir-task-version": 1,
            "type": "prove",
            "target_file": "Foo.lean",
            "target_decl": "foo_bar",
            "project_ref": _PROJECT_REF,
            "deps": [],
        }
    )


def test_prove_next_steps_use_profile_build_command(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    _print_claim_next_steps(tmp_path, _prove_record(), ROCQ)
    out = capsys.readouterr().out
    assert "dune build" in out
    assert "lake build" not in out
    assert "choir submit" in out
    assert "Foo.lean" in out


def test_prove_next_steps_default_lean4_build_command(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    _print_claim_next_steps(tmp_path, _prove_record(), LEAN4)
    out = capsys.readouterr().out
    assert "lake build" in out


# ---------------------------------------------------------------------------
# `choir update`
# ---------------------------------------------------------------------------


def test_cmd_update_prints_shas_when_changed(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        cli_mod,
        "run_update",
        lambda: UpdateResult(old_sha="aaaaaaa1", new_sha="bbbbbbb2", changed=True),
    )

    rc = cli_mod.cmd_update(Namespace(json=False))

    out = capsys.readouterr().out
    assert rc == 0
    assert "aaaaaaa" in out
    assert "bbbbbbb" in out


def test_cmd_update_prints_already_up_to_date(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        cli_mod,
        "run_update",
        lambda: UpdateResult(old_sha="aaaaaaa1", new_sha="aaaaaaa1", changed=False),
    )

    rc = cli_mod.cmd_update(Namespace(json=False))

    out = capsys.readouterr().out
    assert rc == 0
    assert "already up to date" in out


def test_cmd_update_error_exits_1(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    def boom() -> UpdateResult:
        raise UpdateError("dirty checkout")

    monkeypatch.setattr(cli_mod, "run_update", boom)

    rc = cli_mod.cmd_update(Namespace(json=False))

    err = capsys.readouterr().err
    assert rc == 1
    assert "dirty checkout" in err


def test_update_subcommand_wired_into_main(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        cli_mod,
        "run_update",
        lambda: UpdateResult(old_sha="aaaaaaa1", new_sha="aaaaaaa1", changed=False),
    )

    rc = cli_mod.main(["--text", "update"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "already up to date" in out


# ---------------------------------------------------------------------------
# `cmd_claim`'s interactive protocol-gate prompt (note 13 §6)
# ---------------------------------------------------------------------------


def test_cmd_claim_protocol_skip_non_tty_prints_hint_no_prompt(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    result = ClaimResult(ClaimOutcome.SKIPPED, reason=_PROTOCOL_REASON)
    monkeypatch.setattr(cli_mod, "claim", lambda repo, issue: result)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    update_calls: list[None] = []
    monkeypatch.setattr(cli_mod, "run_update", lambda: update_calls.append(None))

    rc = cli_mod.cmd_claim(Namespace(repo="acme/proofs", issue=9, json=False))

    out = capsys.readouterr().out
    assert rc == 2
    assert "protocol" in out
    assert "choir update" in out
    assert update_calls == []


def test_cmd_claim_protocol_skip_tty_default_yes_updates_no_retry(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    claim_calls: list[tuple[str, int]] = []

    def fake_claim(repo: str, issue: int) -> ClaimResult:
        claim_calls.append((repo, issue))
        return ClaimResult(ClaimOutcome.SKIPPED, reason=_PROTOCOL_REASON)

    monkeypatch.setattr(cli_mod, "claim", fake_claim)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "")  # empty = default yes
    monkeypatch.setattr(
        cli_mod,
        "run_update",
        lambda: UpdateResult(old_sha="aaaaaaa1", new_sha="bbbbbbb2", changed=True),
    )

    rc = cli_mod.cmd_claim(Namespace(repo="acme/proofs", issue=9, json=False))

    out = capsys.readouterr().out
    assert rc == 2
    assert claim_calls == [("acme/proofs", 9)]  # never retried in-process
    assert "aaaaaaa" in out
    assert "bbbbbbb" in out
    assert "re-run the claim" in out


def test_cmd_claim_protocol_skip_tty_explicit_y_updates(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    result = ClaimResult(ClaimOutcome.SKIPPED, reason=_PROTOCOL_REASON)
    monkeypatch.setattr(cli_mod, "claim", lambda repo, issue: result)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "Y")
    update_calls: list[None] = []

    def fake_run_update() -> UpdateResult:
        update_calls.append(None)
        return UpdateResult(old_sha="aaaaaaa1", new_sha="bbbbbbb2", changed=True)

    monkeypatch.setattr(cli_mod, "run_update", fake_run_update)

    rc = cli_mod.cmd_claim(Namespace(repo="acme/proofs", issue=9, json=False))

    assert rc == 2
    assert update_calls == [None]


def test_cmd_claim_protocol_skip_tty_no_declines_update(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    result = ClaimResult(ClaimOutcome.SKIPPED, reason=_PROTOCOL_REASON)
    monkeypatch.setattr(cli_mod, "claim", lambda repo, issue: result)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    update_calls: list[None] = []
    monkeypatch.setattr(cli_mod, "run_update", lambda: update_calls.append(None))

    rc = cli_mod.cmd_claim(Namespace(repo="acme/proofs", issue=9, json=False))

    out = capsys.readouterr().out
    assert rc == 2
    assert update_calls == []
    assert "choir update" in out


def test_cmd_claim_protocol_skip_update_error_exits_1(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    result = ClaimResult(ClaimOutcome.SKIPPED, reason=_PROTOCOL_REASON)
    monkeypatch.setattr(cli_mod, "claim", lambda repo, issue: result)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")

    def boom() -> UpdateResult:
        raise UpdateError("dirty checkout")

    monkeypatch.setattr(cli_mod, "run_update", boom)

    rc = cli_mod.cmd_claim(Namespace(repo="acme/proofs", issue=9, json=False))

    err = capsys.readouterr().err
    assert rc == 1
    assert "dirty checkout" in err


def test_cmd_claim_non_protocol_skip_never_checks_tty(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    """A SKIPPED reason with no 'protocol' word must not trigger the prompt
    path at all — not even a tty check, let alone reading stdin."""
    result = ClaimResult(ClaimOutcome.SKIPPED, reason="already assigned to bob")
    monkeypatch.setattr(cli_mod, "claim", lambda repo, issue: result)

    def boom() -> bool:
        raise AssertionError("should not check tty for a non-protocol skip")

    monkeypatch.setattr(sys.stdin, "isatty", boom)

    rc = cli_mod.cmd_claim(Namespace(repo="acme/proofs", issue=9, json=False))

    out = capsys.readouterr().out
    assert rc == 2
    assert "already assigned" in out


def test_protocol_prompt_ctrl_d_declines_cleanly(monkeypatch, capsys):
    # Ctrl-D (EOFError) at the update prompt is a decline, not a traceback
    # (task-3 review follow-up).
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    def _eof(_prompt: str) -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", _eof)
    rc = cli_mod._handle_protocol_stale_skip(
        ClaimResult(ClaimOutcome.SKIPPED, "project requires protocol 3; ...")
    )
    out = capsys.readouterr().out
    assert rc == 2
    assert "choir update" in out


# --- `--json`: the outcome is in the payload, never in the exit code -------
# This is the property that made the library the documented agent surface
# (commit df6cecc: "claim returns a ClaimResult whose outcome an agent
# branches on, where the CLI gives an exit code and prose"). A JSON CLI
# carries the same distinction, so it has to be held to it.

def test_json_claim_lost_race_exits_zero_with_outcome_in_payload(
    monkeypatch, capsys
) -> None:  # type: ignore[no-untyped-def]
    """LOST_RACE means "someone got there first, take another task" — routine,
    not a failure. Prose mode returns 1; under --json that would be
    indistinguishable from a real error, so it must be 0."""
    monkeypatch.setattr(
        cli_mod,
        "claim",
        lambda repo, issue: ClaimResult(
            outcome=ClaimOutcome.LOST_RACE, reason="held by @alice", winner="alice"
        ),
    )

    rc = cli_mod.cmd_claim(Namespace(repo="acme/proofs", issue=9, json=True))

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["outcome"] == "LOST_RACE"
    assert payload["winner"] == "alice"
    assert payload["prepared"] is None


def test_json_claim_never_prompts_on_protocol_skip(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    """The prose path offers an interactive update. An agent has no tty to
    answer it, so --json reports the outcome and a hint instead of blocking."""
    monkeypatch.setattr(
        cli_mod,
        "claim",
        lambda repo, issue: ClaimResult(
            outcome=ClaimOutcome.SKIPPED, reason=_PROTOCOL_REASON, winner=None
        ),
    )

    def _explode() -> None:
        raise AssertionError("--json must not prompt")

    monkeypatch.setattr("builtins.input", lambda *a, **k: _explode())

    rc = cli_mod.cmd_claim(Namespace(repo="acme/proofs", issue=9, json=True))

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["outcome"] == "SKIPPED"
    assert "choir update" in payload["hint"]


def test_format_defaults_to_json_when_stdout_is_not_a_terminal(
    monkeypatch, capsys
) -> None:  # type: ignore[no-untyped-def]
    """An agent captures stdout, so it gets the machine contract without
    having to know a flag exists. A person at a terminal gets prose; both
    can be forced with --json / --text."""
    monkeypatch.setattr(
        cli_mod,
        "run_update",
        lambda: UpdateResult(old_sha="aaaaaaa1", new_sha="bbbbbbb2", changed=True),
    )

    rc = cli_mod.main(["update"])          # capsys => stdout is not a tty

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["changed"] is True
    assert payload["new_sha"] == "bbbbbbb2"


def _stub_update(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        cli_mod,
        "run_update",
        lambda: UpdateResult(old_sha="aaaaaaa1", new_sha="bbbbbbb2", changed=True),
    )


def test_text_is_honoured_after_the_subcommand(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    """Appending the flag is what people and agents reach for first, so it
    has to mean the same there as before the subcommand."""
    _stub_update(monkeypatch)

    rc = cli_mod.main(["update", "--text"])

    out = capsys.readouterr().out
    assert rc == 0
    assert not out.lstrip().startswith("{")


def test_json_is_honoured_before_the_subcommand(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    _stub_update(monkeypatch)

    rc = cli_mod.main(["--json", "update"])

    assert rc == 0
    assert json.loads(capsys.readouterr().out)["changed"] is True


def test_a_flag_before_the_subcommand_survives_the_subparser(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    """The subcommand copies suppress their default; a `False` default there
    would overwrite a `--text` given before the subcommand."""
    _stub_update(monkeypatch)

    rc = cli_mod.main(["--text", "update"])

    out = capsys.readouterr().out
    assert rc == 0
    assert not out.lstrip().startswith("{")


def test_the_two_format_flags_stay_mutually_exclusive(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _stub_update(monkeypatch)

    with pytest.raises(SystemExit):
        cli_mod.main(["update", "--json", "--text"])


# --- `choir heartbeat` ------------------------------------------------------


def test_cmd_heartbeat_presents_the_session_from_the_workspace(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    """The command must pass the claim's session, not the default `""`.

    Claiming and beating are separate invocations, and the holder check is
    keyed on the `(login, session)` pair, so a beat that omits the session
    is discarded by every reader.
    """
    seen: dict[str, object] = {}

    def fake(repo, issue, *, session=None, start=None, **kw):  # type: ignore[no-untyped-def]
        seen.update(repo=repo, issue=issue, session=session, start=start)
        return True, "0123456789abcdef"

    monkeypatch.setattr(cli_mod, "heartbeat_for_issue", fake)
    args = Namespace(repo="alice/proj", issue=7, session=None, json=False)

    assert cli_mod.cmd_heartbeat(args) == 0
    assert seen["repo"] == "alice/proj" and seen["issue"] == 7
    assert seen["session"] is None
    assert seen["start"] is not None
    assert "lease refreshed" in capsys.readouterr().out


def test_cmd_heartbeat_forwards_an_explicit_session(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    seen: dict[str, object] = {}

    def fake(repo, issue, *, session=None, start=None, **kw):  # type: ignore[no-untyped-def]
        seen["session"] = session
        return True, session

    monkeypatch.setattr(cli_mod, "heartbeat_for_issue", fake)
    args = Namespace(repo="alice/proj", issue=7, session="beefcafe", json=False)

    assert cli_mod.cmd_heartbeat(args) == 0
    assert seen["session"] == "beefcafe"


def test_cmd_heartbeat_distinguishes_unknown_session_from_lost_lease(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        cli_mod, "heartbeat_for_issue", lambda *a, **k: (False, None)
    )
    args = Namespace(repo="alice/proj", issue=7, session=None, json=False)

    assert cli_mod.cmd_heartbeat(args) == 1
    assert "session the claim was made under is unknown" in capsys.readouterr().err

    monkeypatch.setattr(
        cli_mod, "heartbeat_for_issue", lambda *a, **k: (False, "0123456789abcdef")
    )
    assert cli_mod.cmd_heartbeat(args) == 1
    assert "may not hold the lease" in capsys.readouterr().err


def test_cmd_heartbeat_json_reports_the_session(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        cli_mod, "heartbeat_for_issue", lambda *a, **k: (True, "0123456789abcdef")
    )
    args = Namespace(repo="alice/proj", issue=7, session=None, json=True)

    assert cli_mod.cmd_heartbeat(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["refreshed"] is True
    assert payload["session"] == "0123456789abcdef"


def test_heartbeat_session_flag_wired_into_main(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    seen: dict[str, object] = {}

    def fake(repo, issue, *, session=None, start=None, **kw):  # type: ignore[no-untyped-def]
        seen["session"] = session
        return True, session

    monkeypatch.setattr(cli_mod, "heartbeat_for_issue", fake)

    assert cli_mod.main(["--text", "heartbeat", "alice/proj", "7"]) == 0
    assert seen["session"] is None

    assert (
        cli_mod.main(
            ["--text", "heartbeat", "alice/proj", "7", "--session", "beefcafe"]
        )
        == 0
    )
    assert seen["session"] == "beefcafe"
