"""Decide-instance audit (Phase 3, sibling to axiom_honesty).

AGENTS.md non-negotiable lists this alongside the axiom-honesty audit:
PRs that introduce new `Decidable` instances or pull in `Classical.*`
helpers need human review before merge.

Why this is its own audit (not folded into axiom_honesty):

- `native_decide` bypasses the kernel and is caught by axiom_honesty.
- `decide` as a tactic *uses* `Decidable` instances; it's safe iff
  those instances are correct. A new `instance : Decidable P` that
  evaluates wrong is the equivalent of an axiom for trust purposes.
- `Classical.choice` is the foundational axiom of classical logic in
  Lean; `Classical.dec` / `Classical.decide` / `Classical.propDecidable`
  / `Classical.byContradiction` all pull on it.

For projects that want a constructive formalization, any net increase
in these flags the PR for human review.

v0 is regex-based (matches axiom_honesty's approach). Multi-line
instance declarations and references inside type expressions are
known false-positive / false-negative sources; real Lean parsing is
the v1 fix.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class Verdict(Enum):
    CLEAN = "clean"
    INTRODUCED = "introduced"


@dataclass(frozen=True)
class Finding:
    pattern: str
    base_count: int
    head_count: int

    @property
    def delta(self) -> int:
        return self.head_count - self.base_count


# Pattern definitions:
#   `decidable_instance`: any line starting with `instance` and
#                         mentioning Decidable / DecidableEq / etc.
#                         Single-line by design; multi-line declarations
#                         are rare in practice.
#   `classical_choice` : direct use of `Classical.choice` (the axiom
#                        of choice in Lean).
#   `classical_decide` : `Classical.dec`, `Classical.decide`,
#                        `Classical.propDecidable` — implicit classical
#                        decidability.
#   `classical_by_contradiction`: `Classical.byContradiction` (the
#                        equivalent of LEM via choice).
#
# The `decidable_instance` pattern doesn't appear in `_REGEX_PATTERNS`;
# it has its own multi-line scanner (`_count_decidable_instances`)
# because matching `instance ... : Decidable ...` requires looking
# across line breaks. A simple regex was previously single-line, which
# allowed a contributor to bypass the audit by inserting a newline
# between `instance` and `Decidable`.
_REGEX_PATTERNS: dict[str, re.Pattern[str]] = {
    "classical_choice": re.compile(r"\bClassical\.choice\b"),
    "classical_decide": re.compile(
        r"\bClassical\.(?:dec|decide|propDecidable|decEq)\b"
    ),
    "classical_by_contradiction": re.compile(r"\bClassical\.byContradiction\b"),
}

# Names whose appearance in an instance's type-signature counts as a
# "decidability instance" introduction. Covers `Decidable`, `DecidableEq`,
# `DecidablePred`, `DecidableRel`, etc.
_DECIDABLE_NAME_RE = re.compile(r"\bDecidable\w*\b")

# Boundaries that end an instance's header (we scan the declaration's
# header — keyword through `:=` — for the typeclass name). Same set as
# statement_equiv's top-level scanner; consistent across audits.
_OPEN_TO_CLOSE = {"(": ")", "{": "}", "[": "]", "⟨": "⟩"}
_CLOSE_CHARS = set(_OPEN_TO_CLOSE.values())


def _instance_header_spans(file_contents: str) -> list[tuple[int, int]]:
    """Return `(start, end)` offsets for each `instance` declaration's header.

    The header runs from the `instance` keyword to the `:=` at paren-
    depth zero (or to the next top-level `instance`/EOF if no `:=`).
    Matches `instance` at line start (optionally indented), which is
    Lean's syntactic requirement for top-level declarations.
    """
    starts = [
        m.start() for m in re.finditer(r"(?m)^\s*instance\b", file_contents)
    ]
    spans: list[tuple[int, int]] = []
    for idx, start in enumerate(starts):
        # Header ends at the first top-level `:=` we find, or at the
        # start of the next instance declaration (whichever comes
        # first). Bounding by the next start keeps a malformed
        # declaration from absorbing the rest of the file.
        limit = starts[idx + 1] if idx + 1 < len(starts) else len(file_contents)
        end = _scan_header_end(file_contents, start, limit)
        spans.append((start, end))
    return spans


def _scan_header_end(text: str, start: int, limit: int) -> int:
    """Find the `:=` at depth zero within `[start, limit)`, or `limit`."""
    depth = 0
    i = start
    while i < limit:
        c = text[i]
        if c in _OPEN_TO_CLOSE:
            depth += 1
            i += 1
            continue
        if c in _CLOSE_CHARS:
            depth = max(0, depth - 1)
            i += 1
            continue
        if c == ":" and depth == 0 and i + 1 < limit and text[i + 1] == "=":
            return i  # exclude the `:=` from the header span
        i += 1
    return limit


def _count_decidable_instances(file_contents: str) -> int:
    """Count `instance` declarations whose header mentions `Decidable*`.

    Multi-line aware: an instance whose `instance` keyword is on one
    line and `Decidable` is several lines later (after newlines, comments,
    or implicit binders) is correctly counted.
    """
    n = 0
    for start, end in _instance_header_spans(file_contents):
        header = file_contents[start:end]
        if _DECIDABLE_NAME_RE.search(header):
            n += 1
    return n


def count_patterns(file_contents: str) -> dict[str, int]:
    """Count occurrences of each watched pattern in the file."""
    counts = {
        name: len(p.findall(file_contents)) for name, p in _REGEX_PATTERNS.items()
    }
    counts["decidable_instance"] = _count_decidable_instances(file_contents)
    return counts


def compare(
    base_contents: str, head_contents: str
) -> tuple[Verdict, list[Finding]]:
    """Compare pattern counts; return findings where head > base.

    Findings sorted by pattern name for deterministic output.
    """
    base_counts = count_patterns(base_contents)
    head_counts = count_patterns(head_contents)
    all_patterns = sorted({*_REGEX_PATTERNS, "decidable_instance"})
    findings = [
        Finding(
            pattern=name,
            base_count=base_counts[name],
            head_count=head_counts[name],
        )
        for name in all_patterns
        if head_counts[name] > base_counts[name]
    ]
    if findings:
        return Verdict.INTRODUCED, findings
    return Verdict.CLEAN, []


def format_findings(findings: list[Finding]) -> str:
    """Render findings as a Markdown table for status-check output."""
    if not findings:
        return "No new decidability / classical-logic introductions."
    rows = "\n".join(
        f"| `{f.pattern}` | {f.base_count} | {f.head_count} | **+{f.delta}** |"
        for f in findings
    )
    return (
        "| pattern | base | head | delta |\n"
        "|---|---|---|---|\n"
        f"{rows}"
    )
