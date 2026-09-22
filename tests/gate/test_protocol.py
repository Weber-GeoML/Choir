"""Tests for `gate.protocol` — the repo-pinned protocol version reader.

Mirrors the tmp-workspace pattern in `tests/gate/provers/test_select.py`,
but the reader here never raises: an absent/malformed pin is advisory
metadata gate-side, and enforcement is client-side (design note 13 §3).
"""

from __future__ import annotations

from pathlib import Path

from gate.protocol import PROTOCOL_VERSION, parse_protocol_pin, read_protocol_pin


def test_protocol_version_is_8() -> None:
    """8 puts a `session` in the lease block, below the login.

    7 made the lease a comment (spec D4, open contribution) and keyed the
    holder on the login alone. One account can run several worker sessions
    at once, and the arbiter then reads the second session's claim as the
    holder re-claiming and hands one task to both — observed, with two
    contributors on one login duplicating work. 8 adds the session so a
    worker is a login *and* a session, which is what makes the comment
    thread able to answer "is someone else already on this?" without
    waiting for the orchestrator to flip a label.

    Bumped rather than shipped silently because a mixed thread is not safe:
    a client still on 7 emits no session, so it keeps double-claiming
    against an 8 client. The pin is what lets an overseer require 8 before
    a claim is accepted.

    Deliberately a literal rather than a computed value: the point of the
    pin is that a bump is a decision someone made, so changing it should
    require editing a test that says which decision. Design note 13 §2
    carries what each version means.
    """
    assert PROTOCOL_VERSION == 9


# ---------------------------------------------------------------------------
# parse_protocol_pin — pure text parser
# ---------------------------------------------------------------------------


def test_parse_reads_the_pinned_version() -> None:
    assert parse_protocol_pin("[project]\nchoir_protocol = 2\n") == 2
    assert parse_protocol_pin("[project]\nchoir_protocol = 3\n") == 3


def test_parse_unpinned_defaults_to_1() -> None:
    assert parse_protocol_pin("") == 1
    assert parse_protocol_pin('[automation]\nmerge = "auto"\n') == 1
    assert parse_protocol_pin('[project]\nprover = "lean4"\n') == 1


def test_parse_unusable_value_defaults_to_1() -> None:
    # bool is a subclass of int in Python — guard against True/False
    # silently passing as a protocol version.
    assert parse_protocol_pin("this = is = not = toml\n") == 1
    assert parse_protocol_pin('[project]\nchoir_protocol = "two"\n') == 1
    assert parse_protocol_pin("project = 5\n") == 1
    assert parse_protocol_pin("[project]\nchoir_protocol = true\n") == 1


# ---------------------------------------------------------------------------
# read_protocol_pin — reads a workspace directory directly
# ---------------------------------------------------------------------------


def test_read_pinned_version(tmp_path: Path) -> None:
    (tmp_path / ".choir").mkdir()
    (tmp_path / ".choir" / "project.toml").write_text(
        '[project]\nprover = "lean4"\nchoir_protocol = 2\nchoir_commit = "abc123"\n',
        encoding="utf-8",
    )
    assert read_protocol_pin(tmp_path) == 2


def test_read_absent_file_defaults_to_1(tmp_path: Path) -> None:
    assert read_protocol_pin(tmp_path) == 1


def test_read_garbage_file_defaults_to_1(tmp_path: Path) -> None:
    (tmp_path / ".choir").mkdir()
    (tmp_path / ".choir" / "project.toml").write_text(
        "not = valid = toml = at = all\n", encoding="utf-8"
    )
    assert read_protocol_pin(tmp_path) == 1


def test_read_protocol_pin_tolerates_non_utf8_bytes(tmp_path):
    # UnicodeDecodeError is a ValueError, not OSError — binary garbage in
    # project.toml must read as unpinned, never raise.
    choir = tmp_path / ".choir"
    choir.mkdir()
    (choir / "project.toml").write_bytes(
        b"\xff\xfe\x00\x01[project]\nchoir_protocol = 2\n\x80\x81"
    )
    assert read_protocol_pin(tmp_path) == 1
