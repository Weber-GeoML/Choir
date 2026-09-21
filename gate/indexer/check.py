"""Compare base + head scans, surface introductions with collisions.

The core duplicate-detection logic. Given a base scan (the project's
declarations before the PR) and a head scan (after the PR):

1. Compute newly-introduced declarations — those in head but not in
   base, identified by (file_path, name) pair.
2. For each new declaration, look up its last-segment name in the
   base scan's index.
3. If the name already exists elsewhere in the project, flag it as
   a potential duplicate with the list of pre-existing locations.

v0 uses the last-segment name as the collision key. This catches
"there's already a `theorem add_comm` somewhere — your new one might
duplicate or shadow it" without requiring namespace-aware resolution.
False positives happen when two namespaces legitimately share a
name (e.g., `List.length` and `String.length`); the indexer's output
is informational, not merge-blocking, so this is acceptable.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from gate.indexer.extract import DeclLocation


@dataclass(frozen=True)
class DuplicateFinding:
    """One newly-introduced declaration whose name appears elsewhere."""

    new_decl: DeclLocation
    existing_locations: tuple[DeclLocation, ...]


def index_by_last_segment(decls: list[DeclLocation]) -> dict[str, list[DeclLocation]]:
    """Build a `last_segment → [locations]` map for collision lookup."""
    index: dict[str, list[DeclLocation]] = defaultdict(list)
    for d in decls:
        index[d.last_segment].append(d)
    return dict(index)


def _location_key(d: DeclLocation) -> tuple[str, str]:
    """Identity for an existing-declaration check: same file + same name."""
    return (d.file_path, d.name)


def find_introductions(
    base: list[DeclLocation], head: list[DeclLocation]
) -> list[DeclLocation]:
    """Declarations in head not present in base (by file + name)."""
    base_keys = {_location_key(d) for d in base}
    return [d for d in head if _location_key(d) not in base_keys]


def find_collisions(
    introductions: list[DeclLocation],
    base: list[DeclLocation],
) -> list[DuplicateFinding]:
    """Cross-reference introductions against existing names in the base.

    A collision is when a newly-introduced declaration's last-segment
    name already appears in the base scan at a different
    (file, name) location. Listing the same declaration as a
    self-collision is filtered out.
    """
    base_index = index_by_last_segment(base)
    findings: list[DuplicateFinding] = []
    for new in introductions:
        existing = base_index.get(new.last_segment, [])
        # Exclude any "existing" entry that's actually the same decl
        # we're introducing (defensive — shouldn't happen since
        # introductions are by definition absent from base).
        same_key = _location_key(new)
        elsewhere = tuple(d for d in existing if _location_key(d) != same_key)
        if elsewhere:
            findings.append(
                DuplicateFinding(
                    new_decl=new,
                    existing_locations=elsewhere,
                )
            )
    return findings


def format_findings(findings: list[DuplicateFinding]) -> str:
    """Render findings as a Markdown report for the PR comment."""
    if not findings:
        return "No potential duplicates found."
    lines = [
        "**Coherence indexer**: potential duplicate names found.",
        "",
        "Each newly-introduced declaration below shares its name "
        "(last segment) with one or more existing declarations elsewhere "
        "in the project. This is **informational, not blocking** — "
        "namespaces legitimately share names. Review whether your new "
        "declaration is actually a duplicate or just a same-named "
        "thing in a different context.",
        "",
    ]
    for f in findings:
        new = f.new_decl
        lines.append(
            f"- **`{new.name}`** introduced at "
            f"`{new.file_path}:{new.line}` — also exists at:"
        )
        for loc in f.existing_locations:
            lines.append(f"  - `{loc.file_path}:{loc.line}` (`{loc.name}`)")
    return "\n".join(lines)
