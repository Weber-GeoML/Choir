"""Extract top-level declarations from Lean source files.

Reuses `gate.provers.decl_syntax.decl_line_regex` but returns the shape
the indexer needs: a mapping from declaration name to locations. Names
go through `normalize_decl_name`, so a no-space-colon declaration
(`lemma foo: "P x"`, Isabelle's dominant house style) reports `foo`.

For namespace-aware fully-qualified names, see the v1 plan in
`gate.indexer.__init__`. v0 captures the literal token after
the declaration keyword (normalized), which may or may not be
fully-qualified depending on whether the declaration is inside a
`namespace` block.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from gate.provers.base import ProverProfile
from gate.provers.decl_syntax import (
    continuation_lines_for,
    decl_line_regex_for,
    decl_name_from,
)
from gate.provers.lean4 import LEAN4
from gate.verify.style import comment_stripped_lines


@dataclass(frozen=True)
class DeclLocation:
    """One declaration occurrence."""

    name: str            # the normalized token after the keyword
    file_path: str       # repo-relative path
    line: int            # 1-indexed

    @property
    def last_segment(self) -> str:
        """The part after the last dot. `Foo.bar.baz` → `baz`."""
        return self.name.rsplit(".", 1)[-1]


def extract_declarations(
    file_contents: str, file_path: str, *, profile: ProverProfile = LEAN4
) -> list[DeclLocation]:
    """Find every top-level declaration in `file_contents`.

    `profile` supplies the declaration keywords, modifiers, and
    attribute-list syntax to match against (defaults to lean4, so
    existing callers are unaffected).
    """
    decl_re = decl_line_regex_for(profile)
    out: list[DeclLocation] = []
    # Scan comment-blanked lines, not raw ones. A declaration line inside a
    # `/- ... -/` block is not a declaration, and matching it produces a
    # phantom entry — which in the sibling statement-immutability audit was
    # simultaneously a false block (the phantom collided with the live
    # declaration's name) and a bypass (it stood in for a deleted one). The
    # indexer's stakes are lower (duplicate-name flagging, not a merge
    # gate), but it is the same defect, and `comment_stripped_lines`
    # guarantees the returned lines stay index- and column-aligned with the
    # real file, so reported line numbers are unaffected.
    stripped_lines = comment_stripped_lines(file_contents, profile=profile)
    # Round 8, F3: a line that begins inside an unterminated multi-line
    # quoted region is not a declaration start. Empty for lean4/rocq.
    continuation = continuation_lines_for(profile, stripped_lines)
    for line_no, line in enumerate(stripped_lines, start=1):
        if line_no - 1 in continuation:
            continue
        match = decl_re.match(line)
        if match is not None:
            out.append(
                DeclLocation(
                    name=decl_name_from(match, stripped_lines, line_no - 1),
                    file_path=file_path,
                    line=line_no,
                )
            )
    return out


def scan_directory(
    root: Path,
    *,
    repo_relative_root: Path | None = None,
    profile: ProverProfile = LEAN4,
) -> list[DeclLocation]:
    """Scan all of the profile's source files under `root` (`.lean` by default).

    `repo_relative_root` controls how `file_path` is computed in the
    returned locations: paths are made relative to this root so the
    output is portable (doesn't leak the absolute checkout path).
    Defaults to `root`.
    """
    base = repo_relative_root if repo_relative_root is not None else root
    out: list[DeclLocation] = []
    paths: set[Path] = set()
    for ext in profile.file_extensions:
        paths.update(root.rglob(f"*{ext}"))
    for path in sorted(paths):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        rel = path.relative_to(base)
        out.extend(extract_declarations(text, str(rel), profile=profile))
    return out
