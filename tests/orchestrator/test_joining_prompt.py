"""Tests for `orchestrator.joining_prompt` — the hardcoded contributor prompt."""

from __future__ import annotations

from orchestrator.joining_prompt import DEFAULT_CHOIR_URL, joining_prompt, main


def test_contains_repo_everywhere_needed() -> None:
    text = joining_prompt("alice/proj")
    assert "**`alice/proj`**" in text
    assert "join.sh alice/proj" in text



def test_clones_under_current_folder_not_home() -> None:
    text = joining_prompt("alice/proj")
    # Clone under the contributor's current agent folder, not a shared ~/choir,
    # so multiple contributors on one machine stay isolated from each other.
    assert "~/choir" not in text
    assert "./choir" in text



def test_choir_url_default_and_override() -> None:
    assert DEFAULT_CHOIR_URL in joining_prompt("alice/proj")
    custom = joining_prompt("alice/proj", choir_url="git@example.com:fork/choir.git")
    assert "git@example.com:fork/choir.git" in custom
    assert DEFAULT_CHOIR_URL not in custom


def test_main_prints_prompt(capsys) -> None:
    assert main(["alice/proj"]) == 0
    out = capsys.readouterr().out
    assert "**`alice/proj`**" in out


def test_main_requires_repo(capsys) -> None:
    assert main([]) != 0


