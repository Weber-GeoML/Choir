"""Pure scanning logic for the trust-boundary inventory.

Shares the regex heuristics of `gate.verify.axiom_honesty` (axiom
names) and `gate.indexer.extract` (declaration headers), but strips
comments first — the inventory is a planning document and false
positives from doc-strings ("this lemma replaces a sorry") would make
it useless. `axiom_honesty` matches the raw text; that is a choice
about its own input, not a general property of over-counting. An audit
comparing a base count against a head count has the opposite failure
direction — a count a comment inflates on the *base* side masks a real
occurrence added at head — which is why `gate.verify.sorry_delta`
counts through this scan rather than matching raw text itself.

Comment stripping, declaration-boundary attribution, and the
placeholder scan are all parameterized by a `gate.provers.ProverProfile`
(default `lean4`, so every existing caller/test is unchanged — design
note 12 §2/§3). Axiom-name inventorying (`AxiomItem`) stays lean4's
`axiom NAME` syntax only; the other two profiles have no analogous
inventory need yet (their trust-model assumptions surface through
`axiom_honesty`'s profile-driven `trust_patterns`, not this module).

Declaration-boundary matching is
`gate.provers.decl_syntax.decl_line_regex_for`, the single source for
every consumer.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from pathlib import Path

from gate.provers.base import CommentSyntax, ProverProfile
from gate.provers.decl_syntax import (
    continuation_lines_for,
    decl_line_regex_for,
    decl_name_from,
)
from gate.provers.lean4 import LEAN4

# Directories never scanned. `.lake` holds build artifacts and vendored
# dependency sources (all of Mathlib!) — inventorying those would bury
# the project's own trust boundary in thousands of upstream hits.
_EXCLUDED_DIR_NAMES = {".lake", ".git", "lake-packages", "build", "_build"}

_AXIOM_RE = re.compile(r"^\s*axiom\s+([A-Za-z_][A-Za-z0-9_.']*)")


# Matches nothing. An empty alternation would compile to `\b(?:)\b`,
# which matches the empty string everywhere — for the placeholder audit
# that reads every declaration as an unfilled placeholder. No shipped
# profile has an empty tuple; this keeps the degenerate case failing safe.
_NEVER_RE = re.compile(r"(?!)")


def placeholder_regex(placeholder_tokens: tuple[str, ...]) -> re.Pattern[str]:
    """Whole-word alternation over a profile's placeholder spellings.

    Whole-word so rocq's `admit` does not match `admit_lemma` and lean4's
    `sorry` does not match `sorryAx`.
    """
    if not placeholder_tokens:
        return _NEVER_RE
    alternation = "|".join(re.escape(t) for t in placeholder_tokens)
    return re.compile(rf"\b(?:{alternation})\b")


def _blank_range(out: list[str], text: str, start: int, end: int) -> None:
    """Overwrite `out[start:end]` with spaces, preserving any newlines."""
    for k in range(start, end):
        if text[k] != "\n":
            out[k] = " "


def _skip_verbatim_span(text: str, start: int, open_: str, close_: str) -> int:
    r"""Return the index just past the verbatim span opening at `start`.

    `start` points at `open_`. The span nests (isabelle cartouches do),
    and *nothing inside it is a comment marker or a string delimiter* —
    which is why this is a plain skip with no blanking: see
    `CommentSyntax.verbatim_delimiters`. An unterminated span runs to
    end of text, matching the pre-existing behaviour for an
    unterminated block comment.
    """
    depth = 1
    i = start + len(open_)
    n = len(text)
    while i < n and depth > 0:
        if text.startswith(open_, i):
            depth += 1
            i += len(open_)
        elif text.startswith(close_, i):
            depth -= 1
            i += len(close_)
        else:
            i += 1
    return i


def _blank_block_comment(
    out: list[str], text: str, start: int, block_open: str, block_close: str
) -> int:
    """Blank the nested block comment opening at `start`; return its end.

    `start` points at `block_open`. Only the comment's own delimiters
    are significant inside it — every other character (a string quote, a
    verbatim/cartouche opener) is ordinary comment text, which is the
    outer half of the precedence rule `strip_comments` documents. An
    unterminated comment runs to end of text.
    """
    open_len, close_len = len(block_open), len(block_close)
    depth = 1
    i = start + open_len
    n = len(text)
    while i < n and depth > 0:
        if text.startswith(block_open, i):
            depth += 1
            i += open_len
        elif text.startswith(block_close, i):
            depth -= 1
            i += close_len
        else:
            i += 1
    _blank_range(out, text, start, i)
    return i


def _skip_string_literal(text: str, start: int) -> int:
    """Return the index just past the `"..."` string literal starting at `start`.

    `start` points at the opening `"`. Handles `\\`-escaped characters
    (including an escaped closing quote) so the scan doesn't end early.
    """
    n = len(text)
    i = start + 1
    while i < n and text[i] != '"':
        i += 2 if text[i] == "\\" and i + 1 < n else 1
    return i + 1


def strip_comments(
    text: str, *, comment_syntax: CommentSyntax = LEAN4.comment_syntax
) -> str:
    r"""Blank out comments while preserving line/column structure.

    Handles `comment_syntax.line` line comments (skipped entirely for
    provers with no line-comment syntax, e.g. isabelle/rocq's `None`),
    nested `comment_syntax.block_open`/`block_close` block comments, and
    skips over `"..."` string literals so a comment marker inside a
    string isn't treated as a comment. Comment characters are replaced
    with spaces, so line numbers and column offsets in the stripped text
    match the original.

    `comment_syntax.verbatim_delimiters` — isabelle's cartouche,
    `\<open>…\<close>`, `None` for lean4 and rocq — is a nesting bracket
    whose content the prover's own lexer does not scan for comment
    markers, so inside it a `block_open` is ordinary text. Read that
    field's docstring before touching this: without it one `\<open>(*)\<close>`
    (HOL's multiplication operator) blanked every declaration to the end
    of the file, and an invisible declaration's statement can be
    rewritten past a blocking check.

    **Precedence between the two, both verified against
    Isabelle2025-2's own build rather than reasoned about:** a comment
    wins on the outside (`(* … \<open> … *)` closes normally — the
    cartouche opener inside a comment is inert), and a cartouche wins on
    the inside (`\<open>(*)\<close>` and even the unbalanced
    `\<^verbatim>\<open>(*\<close>` of `Doc/Isar_Ref/Document_Preparation.thy`
    build fine — the comment opener inside a cartouche is inert). A
    `"` inside a cartouche is likewise ordinary text, matching
    `gate.provers.isabelle._scan_line`'s model: Isabelle prose is full
    of unbalanced quotes inside cartouches
    (`subsubsection\<open>… All Parts" of a Message\<close>`), and
    treating that one as a string opener suppresses comment detection
    for everything up to the next `"` in the file.

    **There is a second blanker**, `gate.provers.isabelle.
    _blank_isabelle_comments`, serving that module's statement
    extractor. The two agree on every shape tried except Isabelle's
    marginal `\<comment> \<open>…\<close>` comment, which that one blanks
    and this one does not. Read its `_BLANKER_DUPLICATION` note before
    unifying them: merging measured **-15 real declarations** across the
    Isabelle2025-2 distribution against +2, and the blocker is
    multi-line declaration-name capture, not the blankers themselves.
    """
    line_marker = comment_syntax.line
    block_open = comment_syntax.block_open
    block_close = comment_syntax.block_close
    verbatim = comment_syntax.verbatim_delimiters
    has_verbatim = verbatim is not None
    # Never `startswith("")` — an empty needle matches everywhere, so the
    # sentinel pair is only ever read behind `has_verbatim`.
    verbatim_open, verbatim_close = verbatim if verbatim is not None else ("", "")
    line_len = len(line_marker) if line_marker is not None else 0

    out = list(text)
    i = 0
    n = len(text)
    while i < n:
        if text.startswith(block_open, i):
            i = _blank_block_comment(out, text, i, block_open, block_close)
            continue
        if line_marker is not None and text[i : i + line_len] == line_marker:
            j = text.find("\n", i)
            if j == -1:
                j = n
            _blank_range(out, text, i, j)
            i = j
            continue
        if has_verbatim and text.startswith(verbatim_open, i):
            # Inside a cartouche only its own delimiters mean anything, so
            # skipping the whole span is the same rule as tracking depth.
            i = _skip_verbatim_span(text, i, verbatim_open, verbatim_close)
            continue
        if has_verbatim and text.startswith(verbatim_close, i):
            # A closer with no opener: swallow it rather than going
            # negative (an unbalanced cartouche does not parse, so this
            # is defensive, not a shape to model).
            i += len(verbatim_close)
            continue
        if text[i] == '"':
            # Skip the string literal (keep its contents — they're code,
            # not comments) so a comment marker inside it isn't misread.
            i = _skip_string_literal(text, i)
            continue
        i += 1
    return "".join(out)


@dataclass(frozen=True)
class AxiomItem:
    """One `axiom NAME ...` declaration in the project's own sources."""

    name: str
    file: str
    line: int  # 1-indexed


@dataclass(frozen=True)
class SorryItem:
    """One `sorry` occurrence, attributed to its enclosing declaration."""

    decl: str | None  # nearest preceding declaration header, if any
    file: str
    line: int  # 1-indexed


@dataclass(frozen=True)
class TrustBoundary:
    """The full inventory for a scanned tree."""

    axioms: tuple[AxiomItem, ...]
    sorries: tuple[SorryItem, ...]
    files_scanned: int

    def as_dict(self) -> dict[str, object]:
        return {
            "axioms": [asdict(a) for a in self.axioms],
            "sorries": [asdict(s) for s in self.sorries],
            "summary": {
                "axiom_count": len(self.axioms),
                "sorry_count": len(self.sorries),
                "files_scanned": self.files_scanned,
            },
        }


def _iter_decl_boundaries(
    stripped_lines: list[str], *, profile: ProverProfile
) -> Iterator[tuple[int, str | None]]:
    """Yield `(line_no, decl_name)` for each line of comment-stripped text.

    `decl_name` is set on a line that starts a declaration and `None`
    otherwise. A line continuing an unterminated multi-line quote is
    never a declaration start; empty continuation set for a profile
    with no `multiline_quote`.

    `scan_text` and `declaration_names` both walk this one pass, which is
    what keeps a name attributed by one comparable to a name enumerated
    by the other — they cannot drift apart because there is only one
    naming path to drift from.
    """
    decl_re = decl_line_regex_for(profile)
    continuation = continuation_lines_for(profile, stripped_lines)
    for line_no, line in enumerate(stripped_lines, start=1):
        if line_no - 1 in continuation:
            yield line_no, None
            continue
        match = decl_re.match(line)
        yield line_no, (
            decl_name_from(match, stripped_lines, line_no - 1)
            if match is not None
            else None
        )


def scan_text(
    text: str, file_path: str, *, profile: ProverProfile = LEAN4
) -> tuple[list[AxiomItem], list[SorryItem]]:
    """Inventory one file's contents (comments stripped first).

    `profile` supplies the comment syntax, declaration keywords and
    prefix shapes (for attributing a placeholder to its enclosing decl
    — prefix-aware via
    `gate.provers.decl_syntax.decl_line_regex_for`, so a prefixed
    declaration is still recognized as its own boundary), and
    placeholder tokens to scan for; defaults to lean4
    so existing callers are unaffected. Axiom-name inventorying stays
    lean4's `axiom NAME` syntax regardless of profile — see module
    docstring.
    """
    stripped = strip_comments(text, comment_syntax=profile.comment_syntax)
    placeholder_re = placeholder_regex(profile.placeholder_tokens)
    stripped_lines = stripped.splitlines()
    axioms: list[AxiomItem] = []
    sorries: list[SorryItem] = []
    current_decl: str | None = None
    for line_no, decl_name in _iter_decl_boundaries(stripped_lines, profile=profile):
        line = stripped_lines[line_no - 1]
        if decl_name is not None:
            current_decl = decl_name
        axiom_match = _AXIOM_RE.match(line)
        if axiom_match is not None:
            axioms.append(
                AxiomItem(name=axiom_match.group(1), file=file_path, line=line_no)
            )
        if placeholder_re.search(line):
            sorries.append(
                SorryItem(decl=current_decl, file=file_path, line=line_no)
            )
    return axioms, sorries


def declaration_names(text: str, *, profile: ProverProfile = LEAN4) -> frozenset[str]:
    """Every declaration name in `text`, named as `scan_text` names them.

    Names are returned exactly as written on the declaration line — the
    same value `SorryItem.decl` carries, because both come from
    `_iter_decl_boundaries`. That is not the same thing as unqualified: a
    declaration written with its own namespace prefix on the line
    (`theorem NS.helper …`) is returned exactly that way, prefix
    included; only a *surrounding* `namespace NS` block does not add
    one, because nothing about that block's own line is a declaration
    line. A caller comparing a `SorryItem.decl` against this set is
    comparing like with like; a caller comparing against a
    namespace-qualified name from elsewhere (e.g.
    `gate.verify.statement_immutability`) is not, and that mismatch
    would make every declaration look new. Either kind of caller that
    needs to compare across a possible qualification difference should
    compare on the last dotted segment instead of the raw name — see
    `gate.verify.sorry_delta.compare_reduction`.
    """
    stripped = strip_comments(text, comment_syntax=profile.comment_syntax)
    return frozenset(
        name
        for _line_no, name in _iter_decl_boundaries(stripped.splitlines(), profile=profile)
        if name is not None
    )


def _is_excluded(path: Path, root: Path) -> bool:
    return any(part in _EXCLUDED_DIR_NAMES for part in path.relative_to(root).parts)


def scan_tree(root: Path, *, profile: ProverProfile = LEAN4) -> TrustBoundary:
    """Inventory every project-owned source file under `root`.

    Walks `profile.file_extensions` (`.lean` by default). Excludes
    `.lake/`, `.git/`, and other vendored/build directories so the
    result reflects the project's own trust boundary, not its
    dependencies'.
    """
    axioms: list[AxiomItem] = []
    sorries: list[SorryItem] = []
    files = 0
    paths: set[Path] = set()
    for ext in profile.file_extensions:
        paths.update(root.rglob(f"*{ext}"))
    for path in sorted(paths):
        if not path.is_file() or _is_excluded(path, root):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        files += 1
        rel = str(path.relative_to(root))
        file_axioms, file_sorries = scan_text(text, rel, profile=profile)
        axioms.extend(file_axioms)
        sorries.extend(file_sorries)
    return TrustBoundary(
        axioms=tuple(axioms), sorries=tuple(sorries), files_scanned=files
    )
