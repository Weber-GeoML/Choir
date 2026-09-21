"""Tests for `orchestrator.cli`.

Two properties carry the design and are what these pin down.

**The override is a separate subcommand.** Where a harness allowlists by
command prefix, an override sharing `merge`'s prefix would be granted by
any rule granting merges; where a harness only prompts, the two would look
alike in the prompt. `force` is the
overseer's, not the orchestrator's (`orchestrator/prs.py`, `merge_pr`), so
the two must not share a prefix.

**Outcomes live in the payload, not the exit code.** A gate that refuses is
an answer to "should this merge?", so it exits 0 and says so in JSON;
non-zero is reserved for producing no answer at all.
"""

from __future__ import annotations

import json

import pytest

from orchestrator import cli as cli_mod
from orchestrator.prs import PRError


def _pr(number: int = 7) -> object:
    class _P:
        pass

    p = _P()
    p.number = number  # type: ignore[attr-defined]
    return p


def test_merge_has_no_override_argument() -> None:
    """`merge` must not accept the override under any spelling — that is the
    whole reason the two are separate subcommands."""
    for flag in ("--reason", "--force"):
        with pytest.raises(SystemExit):
            cli_mod.main(["--repo", "o/r", "merge", "7", flag, "because"])


def test_merge_override_requires_a_reason() -> None:
    """An override with no attributable reason is exactly what `merge_pr`
    refuses; the CLI should not be able to express it either."""
    with pytest.raises(SystemExit):
        cli_mod.main(["--repo", "o/r", "merge-override", "7"])


def test_merge_passes_no_force_and_override_passes_the_reason(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    seen: list[object] = []
    monkeypatch.setattr(cli_mod, "get_pr", lambda repo, number: _pr(number))
    monkeypatch.setattr(
        cli_mod, "merge_pr",
        lambda repo, number, **kw: seen.append(kw.get("force")),
    )

    cli_mod.main(["--repo", "o/r", "merge", "7"])
    cli_mod.main(["--repo", "o/r", "merge-override", "7", "--reason", "runner outage"])

    assert seen == [None, "runner outage"]


def test_refused_merge_exits_zero_and_says_so(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    """A red gate is a routine answer, not a crash."""
    monkeypatch.setattr(cli_mod, "get_pr", lambda repo, number: _pr(number))

    def _refuse(repo, number, **kw):  # type: ignore[no-untyped-def]
        raise PRError("PR #7 is not green — refusing to merge")

    monkeypatch.setattr(cli_mod, "merge_pr", _refuse)

    rc = cli_mod.main(["--repo", "o/r", "merge", "7"])

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["merged"] is False
    assert "refusing to merge" in payload["refused"]


def test_unreadable_pr_is_infrastructure_failure_not_a_refusal(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    """A PR that cannot be read produced no answer, so it must not be
    reported as the gate having decided something."""
    def _boom(repo, number):  # type: ignore[no-untyped-def]
        raise PRError("could not resolve to a PullRequest")

    monkeypatch.setattr(cli_mod, "get_pr", _boom)

    rc = cli_mod.main(["--repo", "o/r", "merge", "999"])

    assert rc == 2
    assert capsys.readouterr().out == ""


def test_viz_needs_no_repo_and_no_arguments(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    """The point of the command is that standing in the folder is enough.

    `--repo` is required for every subcommand that talks to GitHub about a
    named project; this one discovers the project from the folder, so a
    required `--repo` would defeat it.
    """
    seen: dict[str, object] = {}

    def _fake(where, *, repo, out, scope):  # type: ignore[no-untyped-def]
        seen.update(where=where, repo=repo, out=out, scope=scope)
        return {"out": "choir-proof-tree.html", "nodes": 3}

    monkeypatch.setattr(cli_mod, "_viz_report", _fake)
    assert cli_mod.main(["viz"]) == 0
    assert seen == {"where": None, "repo": None, "out": None, "scope": "plan"}
    assert json.loads(capsys.readouterr().out)["nodes"] == 3


def test_viz_reports_an_unreadable_folder_as_bad_arguments(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    """Standing in the wrong directory is the overseer's mistake to fix.

    It is not a GitHub failure, so it takes exit 1 rather than 2.
    """

    def _fake(*_a, **_k):  # type: ignore[no-untyped-def]
        raise cli_mod.ReportError("not a git checkout")

    monkeypatch.setattr(cli_mod, "_viz_report", _fake)
    assert cli_mod.main(["viz"]) == 1
    assert "not a git checkout" in capsys.readouterr().err


# ------------------------------------------------- where an argument may go
#
# Two spellings of the same invocation have to mean the same thing. Both
# have now cost a session: `--json` after the subcommand on the worker CLI,
# and `--pr 82` where this CLI wanted a bare positional.


@pytest.mark.parametrize("argv", [
    ["--repo", "o/r", "task", "7"],
    ["task", "7", "--repo", "o/r"],
    ["task", "--repo", "o/r", "7"],
])
def test_repo_may_be_written_on_either_side_of_the_subcommand(argv, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    """Appending the flag is what both people and agents reach for first.

    The middle spelling is the one that fails quietly: with `--repo`
    unknown to the subparser, `o/r` is eaten by the `number` positional and
    the error talks about an invalid int.
    """
    monkeypatch.setattr(cli_mod, "get_task", lambda repo, number: {"repo": repo, "number": number})

    assert cli_mod.main(argv) == 0
    assert json.loads(capsys.readouterr().out) == {"repo": "o/r", "number": 7}


def test_a_repo_before_the_subcommand_survives_the_subparser(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    """argparse applies defaults after parsing, so a default on the
    subcommand's copy of the flag would overwrite the value given early."""
    monkeypatch.setattr(cli_mod, "get_task", lambda repo, number: {"repo": repo})

    assert cli_mod.main(["--repo", "o/r", "task", "7"]) == 0
    assert json.loads(capsys.readouterr().out) == {"repo": "o/r"}


def test_the_number_may_be_written_as_a_flag(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    """`salvage` spells it `--pr`, so reaching for `--pr` here is a fair
    guess; it should not be a usage error."""
    seen: dict[str, object] = {}
    monkeypatch.setattr(cli_mod, "post_comment",
                        lambda repo, number, body: seen.update(number=number, body=body))

    assert cli_mod.main(["--repo", "o/r", "comment", "--pr", "7", "--body", "hi"]) == 0
    assert seen == {"number": 7, "body": "hi"}
    assert json.loads(capsys.readouterr().out)["number"] == 7


def test_a_task_number_may_be_written_as_issue(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    """The alias names what the subcommand acts on: task issues take
    `--issue`, pull requests take `--pr`."""
    monkeypatch.setattr(cli_mod, "get_task", lambda repo, number: {"number": number})

    assert cli_mod.main(["--repo", "o/r", "task", "--issue", "7"]) == 0
    assert json.loads(capsys.readouterr().out) == {"number": 7}


def test_omitting_the_number_is_an_error_against_the_subcommand(capsys) -> None:
    """The usage printed has to be the subcommand's. Reported against the
    top-level parser it lists every subcommand instead, which hides the
    argument that was actually missing."""
    with pytest.raises(SystemExit):
        cli_mod.main(["--repo", "o/r", "comment", "--body", "hi"])

    err = capsys.readouterr().err
    assert "choir orch comment" in err
    assert "--pr" in err


def test_giving_the_number_twice_is_an_error(capsys) -> None:
    """Two spellings of one argument, and no way to tell which was meant if
    they disagree."""
    with pytest.raises(SystemExit):
        cli_mod.main(["--repo", "o/r", "comment", "7", "--pr", "7", "--body", "hi"])

    assert "choir orch comment" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [
    ["--repo", "o/r", "set-priority", "7", "high"],
    ["--repo", "o/r", "set-priority", "--issue", "7", "high"],
])
def test_a_second_positional_still_binds_when_the_number_is_optional(
    argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`set-priority` and `set-difficulty` take a value after the number, so
    making the number optional puts two positionals in a row."""
    monkeypatch.setattr(cli_mod, "set_priority", lambda repo, number, value: None)

    assert cli_mod.main(argv) == 0
    assert json.loads(capsys.readouterr().out) == {"number": 7, "priority": "HIGH"}
