"""Style audit (Phase 3 — minimal v0).

AGENTS.md lists "style audit (length, identifier reuse, definition
reuse)" in the verify pipeline. v0 ships the simplest of the three:

- **Length check.** For each top-level declaration in changed files,
  count the lines from its declaration line up to the next top-level
  decl (or EOF). Anything beyond `threshold_lines` (default 200) is
  flagged.

Deferred to future iterations (would benefit from real Lean parsing):

- **Identifier reuse / shadowing.** Detecting that a new local binding
  collides with an existing identifier in scope, or that a definition
  reuses a name already defined elsewhere in the project.
- **Definition reuse.** Detecting that a new definition is structurally
  equivalent to one already in the repo — this is the coherence
  indexer's job (Phase 5), not the style audit's.

The length check is ADVISORY (spec 2026-08-17 D9): it reports over-threshold
declarations for reviewer attention but does not block merge. Length is a soft
signal for the orchestrator's review, not a merge gate — an honest proof may
legitimately be long. Projects tune it via `.choir/verify.toml`
`[audits.style] threshold_lines` (default 200; read from the PR's base SHA)
— domains with genuinely long proofs (heavy algebra/combinatorics) raise it;
teaching repos that want tight proofs lower it.

The compare semantics match the other count-and-compare audits:
*new* long declarations in the head version that weren't long in the
base flag the PR. Pre-existing long declarations stay pre-existing —
the audit doesn't retroactively block PRs that touch a file
containing them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from functools import cache

from gate.inventory.scan import strip_comments
from gate.provers.base import ProverProfile
from gate.provers.decl_syntax import (
    continuation_lines_for,
    decl_line_regex_for,
    decl_name_from,
)
from gate.provers.lean4 import LEAN4

DEFAULT_LINE_THRESHOLD = 200


class Verdict(Enum):
    CLEAN = "clean"
    INTRODUCED = "introduced"


@dataclass(frozen=True)
class DeclSpan:
    name: str
    start_line: int  # 1-indexed
    end_line: int    # 1-indexed, inclusive
    keyword: str  # the decl keyword that opened this span, e.g. "theorem"

    @property
    def line_count(self) -> int:
        return self.end_line - self.start_line + 1


@dataclass(frozen=True)
class Finding:
    name: str
    line_count: int
    threshold: int


# Characters `str.splitlines()` treats as a line break *other* than
# `\n`. `strip_comments` blanks comment content by overwriting each
# character with a space, preserving only `\n` — so a `\r` (or a form
# feed, or U+2028) that happens to fall *inside* a comment would lose
# its line-break status and silently shift every subsequent line's
# index by one. `comment_stripped_lines` restores them, which is what
# makes the "blanked lines index the real file" invariant something
# this module enforces rather than assumes.
_NON_LF_LINE_BOUNDARIES = frozenset(
    "\r\v\f\x1c\x1d\x1e\x85\u2028\u2029"
)


def comment_stripped_lines(
    file_contents: str, *, profile: ProverProfile = LEAN4
) -> list[str]:
    """Return `file_contents`'s lines with comment content blanked out.

    The returned list is aligned 1:1 with `file_contents.splitlines()`
    — same length, same indices, same columns — so a line number read
    off the blanked copy indexes the real file. That alignment is what
    lets every scanner in this module (and
    `gate.verify.statement_immutability`'s namespace qualification)
    look for declarations in text where comments cannot masquerade as
    code, without a second notion of "line number".

    Alignment is *enforced*, not assumed. `strip_comments` overwrites
    comment characters with spaces and never inserts or deletes, so
    positions survive — but it preserves only `\\n`, and
    `str.splitlines()` also breaks on `\\r`, form feeds and a handful
    of Unicode separators. One of those inside a comment would be
    blanked to a space, dropping a line boundary and shifting every
    later index. So any such character is restored from the original
    before splitting, and a length mismatch (impossible today) is
    padded/truncated rather than left to raise or silently misindex.

    `strip_comments` runs once over the whole text rather than per
    line, because block-comment depth is carried across line breaks: a
    multi-line block's interior lines are only recognized as
    still-inside-the-comment by a scan that started at the opener.
    """
    lines = file_contents.splitlines()
    blanked = strip_comments(file_contents, comment_syntax=profile.comment_syntax)
    if len(blanked) == len(file_contents) and any(
        char in _NON_LF_LINE_BOUNDARIES for char in file_contents
    ):
        blanked = "".join(
            original if original in _NON_LF_LINE_BOUNDARIES else stripped
            for original, stripped in zip(file_contents, blanked, strict=True)
        )
    stripped_lines = blanked.splitlines()
    if len(stripped_lines) != len(lines):  # pragma: no cover — defensive
        stripped_lines = (stripped_lines + [""] * len(lines))[: len(lines)]
    return stripped_lines


def _indent_of(line: str) -> int:
    """Column at which `line`'s first non-whitespace character sits."""
    return len(line) - len(line.lstrip())


def find_decl_spans(
    file_contents: str, *, profile: ProverProfile = LEAN4
) -> list[DeclSpan]:
    """Return one `DeclSpan` per top-level declaration in the file.

    **Direction, not coverage — read this before touching `_span_end`
    again.** A span runs from its declaration line through the last line
    the trailing-trim allowlist does not recognize as *not* body, so an
    unrecognized line is attributed to the preceding declaration: the
    *over*-attributing choice, deliberately. The under-attributing
    alternative (body only if indented deeper) was tried and reverted —
    it excluded a `structure`'s or `inductive`'s own column-0 body,
    which is legal Lean 4 (`structure Point where` with column-0 fields
    compiles on v4.32.0), so retyping a field compared equal and
    `statement_immutability` reported `UNCHANGED` on a real change. For
    an advisory check a bypass is the wrong failure mode: a green
    verdict is never read. The two rules do not compose — taking the
    longer span reinstates the false blocks, the shorter one the
    column-zero bypass. This module picks over-attribution.

    **Declarations are matched against the comment-blanked copy of the
    file**, never the raw lines. Otherwise a `theorem foo` line inside a
    `/- … -/` block enumerates as real — which was a bypass, not just
    noise: a worker could delete the target theorem, paste a verbatim
    copy inside a comment, and have the check report `UNCHANGED`.

    `profile` supplies the declaration keywords and every prefix shape
    that marks a decl boundary (default lean4), so `private theorem
    foo`, `@[simp] theorem foo`, `open Nat in theorem foo` and `Time
    Definition foo := 5.` each start their own span. It also supplies
    `_span_end`'s one profile-driven allowlist entry, the standalone
    attribute-list line.

    Anonymous declarations (`example`) are included; their name is
    whatever token follows the keyword, normalized by
    `gate.provers.decl_syntax.normalize_decl_name`.
    """
    decl_re = decl_line_regex_for(profile)
    lines = file_contents.splitlines()
    stripped_lines = comment_stripped_lines(file_contents, profile=profile)
    # Lines that BEGIN inside an unterminated multi-line quoted region are
    # not declaration starts, however much they look like one (round 8,
    # F3). Empty for every profile with no `multiline_quote`.
    continuation = continuation_lines_for(profile, stripped_lines)
    starts: list[tuple[str, str, int]] = []
    for i, line in enumerate(stripped_lines):
        if i in continuation:
            continue
        match = decl_re.match(line)
        if match is not None:
            starts.append(
                (match.group(1), decl_name_from(match, stripped_lines, i), i)
            )

    spans: list[DeclSpan] = []
    for idx, (keyword, name, start_idx) in enumerate(starts):
        next_start = starts[idx + 1][2] if idx + 1 < len(starts) else len(lines)
        end_idx = _span_end(
            stripped_lines,
            start_idx,
            next_start - 1,
            profile=profile,
            continuation=continuation,
        )
        spans.append(
            DeclSpan(
                name=name,
                start_line=start_idx + 1,  # 1-indexed for human-facing output
                end_line=end_idx + 1,
                keyword=keyword,
            )
        )
    return spans


# The trailing-trim allowlist (restored, and extended, by the final
# round of statement-immutability hardening — see `find_decl_spans`'s
# "Direction, not coverage" section for why an allowlist is the chosen
# failure mode and `_span_end` for how it's applied). Lines matching
# this are *never* body: a command that closes or opens a scope, an
# environment-level assumption/notation declaration, or a standalone
# attribute/deriving command that attaches to the *next* declaration
# rather than belonging to the one before it.
#
# Case-insensitive on purpose. Rocq spells its own scope closers
# capitalized (`End Foo.`, `Section Foo.`) where lean4/isabelle write
# `end`/`section` lowercase; matching case-sensitively would silently
# stop recognizing Rocq's spelling and reopen a real, already-fixed
# defect (`End Foo.` glued onto the preceding declaration) as a side
# effect of restoring this allowlist — see
# `test_rocq_end_closer_now_ends_the_span_on_every_profile`.
#
# Do not move `universe`/`notation`/`attribute` out into
# `profile.non_body_commands`: this core is case-insensitive, so it is
# what recognizes isabelle's and rocq's own `notation`/`Notation` (the
# isabelle tuple lists only `no_notation`/`type_notation`), and moving
# them would silently un-recognize both.
#
# `deriving\s+instance\b` (not bare `deriving\b`) is deliberate: Lean's
# *attached* `deriving Repr` clause is legitimately part of a
# `structure`/`inductive`'s own body and never says "instance"
# immediately after `deriving`; only the standalone `deriving instance
# Repr for Point` command does. Matching the bare keyword would
# misclassify the attached form as a trailer.
_NON_BODY_TRAILER_RE = re.compile(
    r"^\s*(?:"
    r"end\b|namespace\b|section\b|import\b|open\b|variable\b|set_option\b"
    r"|universe\b|notation\b|attribute\b"
    r"|deriving\s+instance\b"
    r")",
    re.IGNORECASE,
)


@cache
def _trailer_re(non_body_commands: tuple[str, ...]) -> re.Pattern[str]:
    r"""`_NON_BODY_TRAILER_RE` widened by one profile's own command vocabulary.

    Prover syntax lives in `ProverProfile.non_body_commands`, next to
    the grammar production it was transcribed from; this is the only
    place the two are combined. See `find_decl_spans`' "Direction, not
    coverage" section for why the allowlist shape is what it is.

    Cached on the tuple rather than the profile, so callers re-entering
    `find_decl_spans` per file compile each pattern once.

    Each command is matched with a trailing `\b`, which is what lets
    `syntax:max`, `infixr:30` and isabelle's no-space `text\<open>…`
    resolve — the character after the keyword is a non-word one, so the
    boundary holds. Ordered longest-first so a command that is a prefix
    of another (`builtin_simproc` / `builtin_simproc_decl`) cannot
    shadow it.
    """
    if not non_body_commands:
        return _NON_BODY_TRAILER_RE
    extra = "|".join(
        re.escape(command) + r"\b"
        for command in sorted(non_body_commands, key=len, reverse=True)
    )
    return re.compile(
        _NON_BODY_TRAILER_RE.pattern + rf"|^\s*(?:{extra})", re.IGNORECASE
    )


@cache
def _standalone_attribute_line_re(profile: ProverProfile) -> re.Pattern[str] | None:
    """Regex for a line that is nothing but one profile's attribute list.

    `@[simp]` alone on its own line (lean4) or `#[global]` (rocq) is the
    dominant style for attaching an attribute to the *next* declaration.
    Driven by `profile.attribute_syntax`, so `None` for isabelle, which
    attaches attributes after the name (`lemma foo[simp]:`) and never as
    a standalone command.

    Anchored at both ends, so an attribute-prefixed declaration
    (`@[simp] theorem foo`) is not swallowed here — that shape starts
    its own span via the prefix-aware declaration match.
    """
    if profile.attribute_syntax is None:
        return None
    open_token, close_token = profile.attribute_syntax
    return re.compile(
        rf"^\s*{re.escape(open_token)}[^{re.escape(close_token)}]*{re.escape(close_token)}\s*$"
    )


def _span_end(
    stripped_lines: list[str],
    start_idx: int,
    limit_idx: int,
    *,
    profile: ProverProfile,
    continuation: frozenset[int] = frozenset(),
) -> int:
    """Index of the last line belonging to the declaration at `start_idx`.

    Scans forward to `limit_idx` (the line before the next declaration,
    or EOF). Four things settle a line's fate, checked in order:

    - a blank line, or a line that is entirely comment (its blanked
      copy strips to nothing) — skipped without ending the span and
      without extending it, so a comment between two body lines stays
      inside and a trailing one stays outside;
    - a line indented deeper than the declaration's own line — body,
      whatever it says;
    - a line-initial `|` at or deeper than the declaration's own
      indentation — also body. Every prover writes match/constructor
      alternatives that way, all three legally at column zero, and no
      prover has a *command* that starts with `|`. Explicit rather than
      resting on the coincidence that nothing in the allowlist below
      starts with `|` either.
    - a line matching `_trailer_re(profile.non_body_commands)` or the
      profile's standalone-attribute-line pattern — never body, ends the
      span.

    `continuation` is `continuation_lines_for(profile, stripped_lines)`
    — the 0-based indices of lines that BEGIN inside an unterminated
    multi-line quoted or verbatim region (empty on lean4 and rocq).
    Skipped outright, because isabelle lets a type or term run across
    lines inside `"…"`, so such a line's first token is HOL text, not a
    command: `definition bar :: "nat\n  end " where …` builds, and
    without this its span ended at the word `end`.

    Anything else — *including a line this function has never seen the
    shape of* — is attributed to the body. An allowlist of "lines that
    are not body" is unbounded, so an unnamed command shape is
    over-attributed to the *preceding* declaration. Deliberate
    direction: for an advisory check, false-blocking an untouched
    declaration beats letting a real edit read `UNCHANGED`. Do not
    replace this with an indentation rule — that fails in the opposite
    direction (a column-zero `structure` with column-zero fields
    compiles on v4.32.0, gets a one-line span, and a retyped field
    reads `UNCHANGED`).

    Never returns a value below `start_idx`: a single-line declaration
    stays a single-line declaration.
    """
    decl_indent = _indent_of(stripped_lines[start_idx])
    attribute_re = _standalone_attribute_line_re(profile)
    trailer_re = _trailer_re(profile.non_body_commands)
    end_idx = start_idx
    for i in range(start_idx + 1, limit_idx + 1):
        line = stripped_lines[i]
        content = line.strip()
        if not content:
            continue
        if i in continuation:
            # Inside an open quote/cartouche: this is the declaration's
            # own text, whatever word it starts with.
            end_idx = i
            continue
        indent = _indent_of(line)
        if indent > decl_indent or (content[0] == "|" and indent >= decl_indent):
            end_idx = i
            continue
        if trailer_re.match(line) or (
            attribute_re is not None and attribute_re.match(line)
        ):
            break
        end_idx = i  # unrecognized: over-attribute rather than under-attribute
    return end_idx


def find_long_decls(
    spans: list[DeclSpan], *, threshold: int = DEFAULT_LINE_THRESHOLD
) -> list[Finding]:
    """Return findings for declarations exceeding `threshold` lines."""
    return [
        Finding(name=s.name, line_count=s.line_count, threshold=threshold)
        for s in spans
        if s.line_count > threshold
    ]


def compare(
    base_contents: str,
    head_contents: str,
    *,
    threshold: int = DEFAULT_LINE_THRESHOLD,
    profile: ProverProfile = LEAN4,
) -> tuple[Verdict, list[Finding]]:
    """Flag declarations that are long in head but weren't long in base.

    Pre-existing long declarations stay pre-existing: the audit doesn't
    block a PR for a long decl it didn't introduce. New decls (no
    same-named decl in base) that exceed threshold flag; existing decls
    that grew past the threshold also flag. `profile` selects the
    decl-boundary keywords (defaults to lean4).
    """
    base_long = {f.name: f.line_count for f in find_long_decls(
        find_decl_spans(base_contents, profile=profile), threshold=threshold
    )}
    head_long = find_long_decls(
        find_decl_spans(head_contents, profile=profile), threshold=threshold
    )

    findings = [
        f
        for f in head_long
        if f.name not in base_long or f.line_count > base_long[f.name]
    ]
    if findings:
        return Verdict.INTRODUCED, sorted(findings, key=lambda f: f.name)
    return Verdict.CLEAN, []


def format_findings(findings: list[Finding]) -> str:
    """Render findings as a Markdown table."""
    if not findings:
        return "No new long declarations."
    rows = "\n".join(
        f"| `{f.name}` | {f.line_count} | {f.threshold} | **+{f.line_count - f.threshold}** |"
        for f in findings
    )
    return (
        "| declaration | lines | threshold | over |\n"
        "|---|---|---|---|\n"
        f"{rows}"
    )
