"""Line-targeted writer for the protocol pin in `.choir/project.toml`.

`write_pin` edits only the `choir_protocol` / `choir_commit` lines within
the `[project]` section, leaving every other line — comments, other
keys, other sections — byte-for-byte unchanged. This is the one place
that mutates the pin: `new-project.sh` at bootstrap and
`upgrade-project.sh` on upgrade (design note 13 §3-4). Everything else
only ever reads it (`gate/protocol.py`).

Behavior:
- Existing `choir_protocol`/`choir_commit` lines are updated in place.
- Missing keys are appended to the end of the `[project]` section's body
  (after its last non-blank line, before any trailing blank line or the
  next section header).
- A missing `[project]` section is appended at end-of-file.
- Returns True iff the file's contents actually changed (idempotent: a
  second call with the same values returns False).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_SECTION_RE = re.compile(r"^\[([^\]]+)\]\s*$")
_PROTOCOL_KEY_RE = re.compile(r"^\s*choir_protocol\s*=")
_COMMIT_KEY_RE = re.compile(r"^\s*choir_commit\s*=")


def _protocol_line(protocol: int) -> str:
    return f"choir_protocol = {protocol}"


def _commit_line(commit: str) -> str:
    return f'choir_commit = "{commit}"'


def _find_project_section(lines: list[str]) -> tuple[int | None, int]:
    """Return `(section_start, section_end)` — `section_end` is exclusive.

    `section_start` is the index of the `[project]` header line, or None
    if absent. `section_end` is the index of the next section header
    (or `len(lines)` if `[project]` is the last section / absent).
    """
    section_start = None
    section_end = len(lines)
    for i, line in enumerate(lines):
        m = _SECTION_RE.match(line)
        if not m:
            continue
        if section_start is None:
            if m.group(1) == "project":
                section_start = i
        else:
            section_end = i
            break
    return section_start, section_end


def write_pin(project_toml_path: Path, *, protocol: int, commit: str) -> bool:
    """Write/update the `choir_protocol`/`choir_commit` pin in `[project]`.

    See module docstring for the edit strategy. Returns True iff the
    file's contents changed.
    """
    original = (
        project_toml_path.read_text(encoding="utf-8") if project_toml_path.is_file() else ""
    )
    lines = original.splitlines()

    protocol_target = _protocol_line(protocol)
    commit_target = _commit_line(commit)

    section_start, section_end = _find_project_section(lines)

    protocol_idx = None
    commit_idx = None
    if section_start is not None:
        for i in range(section_start + 1, section_end):
            if _PROTOCOL_KEY_RE.match(lines[i]):
                protocol_idx = i
            elif _COMMIT_KEY_RE.match(lines[i]):
                commit_idx = i

    new_lines = list(lines)

    if protocol_idx is not None:
        new_lines[protocol_idx] = protocol_target
    if commit_idx is not None:
        new_lines[commit_idx] = commit_target

    if section_start is None:
        if new_lines and new_lines[-1].strip() != "":
            new_lines.append("")
        new_lines.append("[project]")
        new_lines.append(protocol_target)
        new_lines.append(commit_target)
    else:
        insertions = []
        if protocol_idx is None:
            insertions.append(protocol_target)
        if commit_idx is None:
            insertions.append(commit_target)
        if insertions:
            # Insert right after the section's last non-blank body line
            # (before any trailing blank separator or the next header).
            insert_at = section_start + 1
            for i in range(section_start + 1, section_end):
                if lines[i].strip() != "":
                    insert_at = i + 1
            new_lines[insert_at:insert_at] = insertions

    new_text = "\n".join(new_lines) + ("\n" if new_lines else "")
    if new_text == original:
        return False

    project_toml_path.parent.mkdir(parents=True, exist_ok=True)
    project_toml_path.write_text(new_text, encoding="utf-8")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Write/update the choir_protocol/choir_commit pin in .choir/project.toml"
    )
    parser.add_argument("path", type=Path, help="Path to .choir/project.toml")
    parser.add_argument("--protocol", type=int, required=True)
    parser.add_argument("--commit", required=True)
    args = parser.parse_args(argv)

    changed = write_pin(args.path, protocol=args.protocol, commit=args.commit)
    print("changed" if changed else "unchanged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
