"""Tests for `gate.upgrade.pin` — the line-targeted protocol-pin writer.

Covers: fresh append (no `[project]` section, or a section missing the
keys), in-place update of existing keys, byte-for-byte preservation of
everything else (comments, other keys, other sections), and idempotency
(second run with the same values is a no-op that returns False).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from gate.upgrade.pin import write_pin


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# fresh append
# ---------------------------------------------------------------------------


def test_write_pin_no_file_creates_new_file(tmp_path: Path) -> None:
    path = tmp_path / "project.toml"
    changed = write_pin(path, protocol=2, commit="abc123")
    assert changed is True
    text = _read(path)
    assert "[project]" in text
    assert "choir_protocol = 2" in text
    assert 'choir_commit = "abc123"' in text


def test_write_pin_creates_project_section_when_absent(tmp_path: Path) -> None:
    path = tmp_path / "project.toml"
    original = '[automation]\nmerge = "auto"\n'
    path.write_text(original, encoding="utf-8")

    changed = write_pin(path, protocol=2, commit="abc123")
    assert changed is True
    text = _read(path)
    assert original in text  # the existing section survives untouched
    assert "[project]" in text
    assert "choir_protocol = 2" in text
    assert 'choir_commit = "abc123"' in text
    # the new section comes after the untouched one
    assert text.index("[automation]") < text.index("[project]")


def test_write_pin_appends_keys_to_existing_project_section(tmp_path: Path) -> None:
    path = tmp_path / "project.toml"
    path.write_text(
        '[project]\nprover = "lean4"\n\n[automation]\nmerge = "auto"\n',
        encoding="utf-8",
    )

    changed = write_pin(path, protocol=2, commit="abc123")
    assert changed is True
    lines = _read(path).splitlines()
    assert lines[0] == "[project]"
    assert lines[1] == 'prover = "lean4"'
    assert lines[2] == "choir_protocol = 2"
    assert lines[3] == 'choir_commit = "abc123"'
    assert "" in lines  # blank separator before [automation] preserved
    assert "[automation]" in lines
    assert 'merge = "auto"' in lines
    # [automation]'s body is untouched
    assert lines.index("[automation]") < lines.index('merge = "auto"')


# ---------------------------------------------------------------------------
# in-place update
# ---------------------------------------------------------------------------


def test_write_pin_updates_existing_keys_in_place(tmp_path: Path) -> None:
    path = tmp_path / "project.toml"
    path.write_text(
        '[project]\nprover = "lean4"\nchoir_protocol = 1\nchoir_commit = "old-sha"\n',
        encoding="utf-8",
    )

    changed = write_pin(path, protocol=2, commit="new-sha")
    assert changed is True
    lines = _read(path).splitlines()
    assert lines[0] == "[project]"
    assert lines[1] == 'prover = "lean4"'  # untouched
    assert lines[2] == "choir_protocol = 2"
    assert lines[3] == 'choir_commit = "new-sha"'
    assert len(lines) == 4  # no lines added — updated in place


def test_write_pin_same_values_no_change(tmp_path: Path) -> None:
    path = tmp_path / "project.toml"
    original = '[project]\nchoir_protocol = 2\nchoir_commit = "abc123"\n'
    path.write_text(original, encoding="utf-8")

    changed = write_pin(path, protocol=2, commit="abc123")
    assert changed is False
    assert _read(path) == original


# ---------------------------------------------------------------------------
# byte-for-byte preservation of everything else
# ---------------------------------------------------------------------------


def test_write_pin_preserves_comments_and_other_keys_byte_for_byte(tmp_path: Path) -> None:
    path = tmp_path / "project.toml"
    original = (
        "# top-of-file comment, do not touch\n"
        "[project]\n"
        "# prover comment\n"
        'prover = "lean4"  # inline comment\n'
        "choir_protocol = 1\n"
        "choir_commit = \"old-sha\"\n"
        "\n"
        "[automation]\n"
        "# automation comment\n"
        'merge = "auto"\n'
        "\n"
        "[reconcile]\n"
        "stale_after_days = 7\n"
    )
    path.write_text(original, encoding="utf-8")

    changed = write_pin(path, protocol=2, commit="new-sha")
    assert changed is True

    new_lines = _read(path).splitlines()
    original_lines = original.splitlines()

    # Every line except the two edited ones is byte-for-byte identical,
    # at the same position.
    edited_indices = {4, 5}  # choir_protocol / choir_commit lines
    for i, line in enumerate(original_lines):
        if i in edited_indices:
            continue
        assert new_lines[i] == line, f"line {i} changed unexpectedly: {line!r}"

    assert new_lines[4] == "choir_protocol = 2"
    assert new_lines[5] == 'choir_commit = "new-sha"'
    assert len(new_lines) == len(original_lines)  # no lines added


# ---------------------------------------------------------------------------
# idempotency
# ---------------------------------------------------------------------------


def test_write_pin_idempotent_second_call_returns_false(tmp_path: Path) -> None:
    path = tmp_path / "project.toml"
    path.write_text('[project]\nprover = "lean4"\n', encoding="utf-8")

    first = write_pin(path, protocol=2, commit="abc123")
    assert first is True
    after_first = _read(path)

    second = write_pin(path, protocol=2, commit="abc123")
    assert second is False
    assert _read(path) == after_first


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_prints_changed_and_exits_0(tmp_path: Path) -> None:
    path = tmp_path / "project.toml"
    path.write_text('[project]\nprover = "lean4"\n', encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "gate.upgrade.pin",
            str(path),
            "--protocol",
            "2",
            "--commit",
            "abc123",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "changed"


def test_cli_prints_unchanged_and_exits_0(tmp_path: Path) -> None:
    path = tmp_path / "project.toml"
    path.write_text(
        '[project]\nchoir_protocol = 2\nchoir_commit = "abc123"\n', encoding="utf-8"
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "gate.upgrade.pin",
            str(path),
            "--protocol",
            "2",
            "--commit",
            "abc123",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "unchanged"
