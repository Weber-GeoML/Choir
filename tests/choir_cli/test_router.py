"""Tests for the `choir` role router.

The router carries one design claim: **the two roles are peers.** Neither
gets the bare command, so `choir` alone orients rather than acting, and
each role's own parser is reached untouched.

It deliberately enforces nothing about *authority*. Which role you may act
in is set by the GitHub token — under spec D4 only the owner has write
access and contributors work from forks — so routing a worker into an
`orch` subcommand is allowed here and refused by GitHub. Having the command
is not having the authority.
"""

from __future__ import annotations

import pytest

import choir_cli.purge as purge_cli
import client.cli as worker_cli
import orchestrator.cli as orch_cli
from choir_cli import main


def test_bare_choir_orients_and_does_not_act(capsys) -> None:  # type: ignore[no-untyped-def]
    rc = main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "worker" in out and "orch" in out
    # Neither role is the default: naming no role must not run one.
    assert "usage: choir <role>" in out


@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_help_lists_both_roles(flag: str, capsys) -> None:  # type: ignore[no-untyped-def]
    assert main([flag]) == 0
    out = capsys.readouterr().out
    assert "CONTRIBUTOR.md" in out and "ORCHESTRATOR.md" in out


def test_unknown_role_is_an_error_not_a_guess(capsys) -> None:  # type: ignore[no-untyped-def]
    rc = main(["orchestrate"])          # close to "orch", must not be guessed at
    err = capsys.readouterr().err
    assert rc == 1
    assert "unknown role" in err


def test_each_role_reaches_its_own_parser(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The router forwards the remaining argv verbatim — it parses nothing
    itself, so neither role's interface is duplicated here."""
    seen: dict[str, list[str]] = {}

    monkeypatch.setattr(worker_cli, "main", lambda argv: seen.setdefault("worker", argv) and 0)
    monkeypatch.setattr(orch_cli, "main", lambda argv: seen.setdefault("orch", argv) and 0)

    main(["worker", "claim", "org/p", "12"])
    main(["orch", "--repo", "org/p", "merge", "12"])

    assert seen["worker"] == ["claim", "org/p", "12"]
    assert seen["orch"] == ["--repo", "org/p", "merge", "12"]


def test_purge_reaches_its_own_parser(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    seen: dict[str, list[str]] = {}
    monkeypatch.setattr(purge_cli, "main", lambda argv: seen.setdefault("purge", argv) and 0)

    main(["purge", "org/p", "--dry-run"])

    assert seen["purge"] == ["org/p", "--dry-run"]


def test_purge_is_listed_without_becoming_a_role(capsys) -> None:  # type: ignore[no-untyped-def]
    """Purge is machine maintenance, not a third role — the two roles stay peers."""
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "purge" in out
    assert "usage: choir <role>" in out


def test_update_reaches_the_worker_parser_as_a_top_level_command(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """`choir update` is maintenance, like `purge` — not a third role.

    It reuses the worker parser's `update` subcommand rather than
    duplicating one, so `--json` and the checkout rules keep a single
    definition. `prog` is what makes `choir update --help` say so.
    """
    seen: dict[str, object] = {}

    def _fake(argv, *, prog="choir worker"):  # type: ignore[no-untyped-def]
        seen["argv"] = argv
        seen["prog"] = prog
        return 0

    monkeypatch.setattr(worker_cli, "main", _fake)

    assert main(["update", "--json"]) == 0
    assert seen["argv"] == ["update", "--json"]
    assert seen["prog"] == "choir"


def test_worker_update_still_works(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Contributors mid-project have `choir worker update` in their docs."""
    seen: dict[str, list[str]] = {}
    monkeypatch.setattr(worker_cli, "main", lambda argv, **kw: seen.setdefault("w", argv) and 0)

    main(["worker", "update"])

    assert seen["w"] == ["update"]


def test_update_is_listed_without_becoming_a_role(capsys) -> None:  # type: ignore[no-untyped-def]
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "update" in out
    assert "usage: choir <role>" in out
