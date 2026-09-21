"""Sorry-delta audit (Phase 3).

Fails PRs that *increase* the number of `sorry`s in any changed `.lean`
file. Closing sorries (the normal work product) passes; smuggling new
ones in — including into declarations the task didn't ask about — does
not.

Why this audit exists: `lake build` treats `sorry` as a *warning*, not
an error, so the clean-room rebuild alone would merge a "proof" that is
still sorry-shaped. Per the 2026-05-07 decision, final-submission sorry
blocks merge. (The `partial_progress` return state — where a checkpoint
with remaining sorries is explicitly accepted and replanned around — is
a v0.5 feature; when it lands, it will be an explicit task-level
escape hatch, not a weakening of this audit.)

Counting uses the inventory's comment-stripped scan, unlike the other
pattern audits: a comment that merely *mentions* sorry must not fail a
contributor's PR. Stripping comments is strictly more accurate here —
commented-out code doesn't elaborate, so it can't hide a real sorry.

Like the other v0 audits this is per-file and regex-based. Out of
scope: `sorryAx` spelled directly, metaprogramming tricks, and what a
string literal does to this scan, since the scan reads a string's
content as ordinary text.

A placeholder token inside a string literal is counted. Because this
audit compares base against head, such a count in the *base* version
masks a real placeholder added at head: a declaration whose base body
holds `"sorry"` and whose head body holds a real `sorry` is one
placeholder on each side, so no rise is reported.

A line shaped like a declaration header inside a string literal is read
as a declaration boundary. A submission can therefore plant a header in
a string and have a placeholder attributed to the declaration that
header names rather than to the one whose body actually contains it.

The general rule is that any line inside a string literal is read as
whatever structure it resembles: a declaration header, a scope opener,
or a scope closer. An opener or a closer misattributes nothing — it
shifts the scope path every declaration after it is qualified under, so
two declarations collide on one count key and a rise from one onto the
other is hidden rather than moved. `compare_reduction` refuses any
placeholder under a count key that more than one head declaration
shares, which is what keeps that shape from granting anything.

For all of these the answers are outside this audit: design note 05's
axiom-trace workflow, which reads the kernel's own dependencies rather
than the text, and the orchestrator's review of the diff.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from enum import Enum

from gate.inventory.scan import (
    SorryItem,
    declaration_names,
    scan_text,
    strip_comments,
)
from gate.provers.base import ProverProfile
from gate.provers.lean4 import LEAN4


class Verdict(Enum):
    CLEAN = "clean"
    INTRODUCED = "introduced"


@dataclass(frozen=True)
class Finding:
    """Sorry counts for one file where head > base."""

    base_count: int
    head_count: int
    head_sorries: tuple[SorryItem, ...]

    @property
    def delta(self) -> int:
        return self.head_count - self.base_count


def count_sorries(
    file_contents: str,
    file_path: str = "<file>",
    *,
    profile: ProverProfile = LEAN4,
) -> list[SorryItem]:
    """All placeholder occurrences in the file, comments stripped.

    `profile` selects which tokens count as placeholders (`sorry` for
    lean4; `sorry`/`oops` for isabelle; `Admitted`/`admit`/`Abort` for
    rocq) and the comment syntax used to strip comments first. Defaults
    to lean4 so existing callers are unchanged.
    """
    _axioms, sorries = scan_text(file_contents, file_path, profile=profile)
    return sorries


def compare(
    base_contents: str,
    head_contents: str,
    *,
    file_path: str = "<file>",
    profile: ProverProfile = LEAN4,
) -> tuple[Verdict, Finding | None]:
    """Compare placeholder counts; INTRODUCED iff head has more than base.

    Reducing or holding the count passes — a PR editing a file that
    already carries placeholders (e.g., progress on one of several) is
    fine as long as it doesn't add new ones.
    """
    base = count_sorries(base_contents, file_path, profile=profile)
    head = count_sorries(head_contents, file_path, profile=profile)
    if len(head) > len(base):
        return Verdict.INTRODUCED, Finding(
            base_count=len(base),
            head_count=len(head),
            head_sorries=tuple(head),
        )
    return Verdict.CLEAN, None


def _decl_leaf(name: str) -> str:
    """The last dotted segment of a declaration name."""
    return name.rsplit(".", 1)[-1]


def _qualified_decls(contents: str, *, profile: ProverProfile) -> dict[int, str]:
    """Each declaration's line mapped to its scope-qualified name.

    `profile.qualify_decl_names` reads a comment-blanked copy — the same
    text `scan_text` attributes placeholders against — and reports a
    declaration's surface name under its enclosing
    `namespace`/`Module`/`locale` path. Empty for a profile that supplies
    no qualifier, which leaves every caller here on surface names.
    """
    if profile.qualify_decl_names is None:
        return {}
    stripped = strip_comments(contents, comment_syntax=profile.comment_syntax)
    return profile.qualify_decl_names(stripped)


def _decl_keys(
    sorries: Collection[SorryItem], qualified: dict[int, str]
) -> list[str | None]:
    """One count key per placeholder, in order; `None` where there is none.

    The key is `qualified`'s name for the placeholder's enclosing
    declaration, so two declarations both written `theorem f`, in sibling
    scopes, are two keys rather than one pooled count a placeholder could
    move within unseen.

    A qualified name is taken only when its last segment is the surface
    name this audit itself attributed the placeholder to; where it is
    not, and where `qualified` has no declaration above the placeholder
    at all, the key is that surface name.

    On the shipped profiles the two declaration walks agree, so neither
    fallback is reached: they share `decl_re`, `decl_name_from` and the
    continuation set, and a qualifier differs only in also consuming
    scope opener and closer lines, which cannot match `decl_re`. The
    fallback is not a safety net. A disagreement is not bounded in the
    refusing direction, so a profile whose qualifier stops agreeing
    with this audit's own boundary scan is a correctness problem here,
    not a graceful degradation.

    A placeholder with no enclosing declaration is not attributable to
    any name, so it gets no key here — the caller refuses it
    unconditionally, on its own, before this ever matters.
    """
    decl_lines = sorted(qualified)
    keys: list[str | None] = []
    for sorry in sorries:
        if sorry.decl is None:
            keys.append(None)
            continue
        key = sorry.decl
        index = bisect_right(decl_lines, sorry.line) - 1
        if index >= 0:
            candidate = qualified[decl_lines[index]]
            if _decl_leaf(candidate) == _decl_leaf(sorry.decl):
                key = candidate
        keys.append(key)
    return keys


def _counts(keys: Iterable[str | None]) -> dict[str, int]:
    """How many times each key occurs; a `None` key is not counted.

    Used both for placeholders per count key and for declaration lines
    per qualified name, which is the same tally over the same key space.
    """
    counts: dict[str, int] = {}
    for key in keys:
        if key is None:
            continue
        counts[key] = counts.get(key, 0) + 1
    return counts


def _leaf_counts(names: Iterable[str]) -> dict[str, int]:
    """How many of `names` share each last dotted segment."""
    counts: dict[str, int] = {}
    for name in names:
        leaf = _decl_leaf(name)
        counts[leaf] = counts.get(leaf, 0) + 1
    return counts


def compare_reduction(
    base_contents: str,
    head_contents: str,
    *,
    target_decl: str,
    children: Collection[str],
    file_path: str = "<file>",
    profile: ProverProfile = LEAN4,
) -> tuple[Verdict, Finding | None]:
    """Compare one file's placeholders under the reduction contract.

    This audit is per-file, exactly like `compare`: it sees only this
    file's base and head contents. A declaration moved into this file
    from another one, or the target itself left sorried in a file the
    PR does not touch, is outside what either function can see.

    `target_decl` is the task's target declaration, resolved by the
    caller from the linked issue that scoped the work — never taken from
    the submission itself. Trusting a submitter to name what their own
    submission is checked against would let the check be steered away
    from whatever the submission actually left unproved.

    Two rules decide whether a head placeholder is allowed, applied in
    this order:

    - **Rule A, absolute.** Any placeholder whose enclosing declaration
      is the target is refused outright, whether or not that
      declaration's own placeholder count rose from base to head. It is
      unconditional because the `comparator` audit permits `sorryAx` on
      this path, so no other mechanical check refuses a target left, or
      newly made, sorried. It is also a textual check: a placeholder
      belongs to the nearest declaration header above it, and a header
      written inside a string literal reads as one, so a submission that
      plants a fabricated header between the target and its own
      placeholder has that placeholder attributed elsewhere and passes
      this rule. The orchestrator's review of the diff is the backstop —
      see the module docstring.
    - **Rule B, delta.** For every other declaration, compare its
      placeholder count at base against head. A count that *rose* is
      refused unless that declaration is absent from this file at base
      **and** is named in `children` — the obligations the submission
      declared it leans on. A count that did not rise is not examined
      at all, so citing an obligation that was already sorried in this
      file at base, and leaving it exactly as sorried, adds no
      placeholder and never trips this rule.

    Counting and membership use different keys, on purpose. Counts are
    keyed on `profile.qualify_decl_names`'s name for a placeholder's
    enclosing declaration — the surface name under its enclosing
    `namespace`/`Module`/`locale` path — computed the same way over base
    and head. So `theorem f` in one namespace block and `theorem f` in a
    sibling block are two counts, and a placeholder cannot move from one
    onto the other inside a single pooled count that Rule B, which
    examines only what rose, would never look at. Qualifying both sides
    equally also means a declaration whose *spelling* changed while its
    proof did not — a dotted prefix moved into a block, or out of one —
    keeps one key and is correctly not a rise.

    The three membership tests compare last dotted segments instead,
    because either spelling may be written on either side: `target_decl`
    comes from the issue, a `children` entry from the submission, and a
    base declaration's own name from a line that may or may not repeat
    its namespace. For the target test and the base test, that coarseness
    only ever refuses: two declarations sharing a last segment read as
    one, which can refuse a placeholder a fully-qualified comparison
    would have allowed, and never the reverse. For the `children` test
    the coarseness runs the *admitting* way — one declared leaf would
    otherwise admit every head declaration sharing it — so a `children`
    entry whose leaf matches more than one declaration in head admits
    none of them. An ambiguous obligation refuses rather than admits.

    A count key is held to the same rule: a key more than one head
    declaration shares refuses every placeholder under it. Rule B
    examines only a key whose count rose, so two declarations pooled
    into one key hide a placeholder moving from the one onto the other.
    Most ways a key can collide are textual and chosen by the
    submission — a scope closer the qualifier does not recognise, one
    sharing a line with a declaration, one planted inside a string
    literal. One is honest: on isabelle an `instantiation`,
    `overloading`, `bundle` or `notepad` block is not tracked as a
    scope, so its `end` pops the enclosing locale and under-qualifies
    every declaration after it — a collision
    `gate.provers.isabelle.qualify_isabelle_decl_names` already records.
    Refusing on ambiguity is still the only direction that cannot be
    steered; the cost is a false block on that isabelle shape.

    A target whose leaf is empty — a name ending in a stray dot — makes
    Rule B refuse every risen count rather than exempt any of them,
    since a degenerate target must fail closed rather than open the
    gate it was meant to narrow. A count that did not rise is still left
    alone even then, per Rule B's own rule above.

    A placeholder with no enclosing declaration never passes: there is
    nothing to attribute it to, and the honest reading is that the
    submission is not the reduction it claims.
    """
    base = count_sorries(base_contents, file_path, profile=profile)
    head = count_sorries(head_contents, file_path, profile=profile)

    target_leaf = _decl_leaf(target_decl)
    base_leaves = {
        _decl_leaf(name) for name in declaration_names(base_contents, profile=profile)
    }
    head_qualified = _qualified_decls(head_contents, profile=profile)
    head_leaf_counts = _leaf_counts(
        head_qualified.values()
        if profile.qualify_decl_names is not None
        else declaration_names(head_contents, profile=profile)
    )
    admitting_child_leaves = {
        _decl_leaf(name)
        for name in children
        if head_leaf_counts.get(_decl_leaf(name), 0) <= 1
    }
    ambiguous_keys = {
        key for key, count in _counts(head_qualified.values()).items() if count > 1
    }

    head_keys = _decl_keys(head, head_qualified)
    base_counts = _counts(
        _decl_keys(base, _qualified_decls(base_contents, profile=profile))
    )
    head_counts = _counts(head_keys)
    risen_decls = {
        decl
        for decl, head_count in head_counts.items()
        if head_count > base_counts.get(decl, 0)
    }
    permitted_decls = (
        set()
        if not target_leaf
        else {
            decl
            for decl in risen_decls
            if _decl_leaf(decl) not in base_leaves
            and _decl_leaf(decl) in admitting_child_leaves
        }
    )
    refused_decls = risen_decls - permitted_decls

    def _is_disallowed(sorry: SorryItem, key: str | None) -> bool:
        if sorry.decl is None:
            return True
        return (
            _decl_leaf(sorry.decl) == target_leaf
            or key in refused_decls
            or key in ambiguous_keys
        )

    disallowed = tuple(
        s for s, key in zip(head, head_keys, strict=True) if _is_disallowed(s, key)
    )
    if disallowed:
        return Verdict.INTRODUCED, Finding(
            base_count=len(base),
            head_count=len(head),
            head_sorries=disallowed,
        )
    return Verdict.CLEAN, None


def format_finding(finding: Finding) -> str:
    """Render one file's finding for status-check output."""
    lines = [
        f"sorries: base {finding.base_count} → head {finding.head_count} "
        f"(**+{finding.delta}**)",
        "",
        "| declaration | line |",
        "|---|---|",
    ]
    for s in finding.head_sorries:
        decl = f"`{s.decl}`" if s.decl else "(no enclosing decl)"
        lines.append(f"| {decl} | {s.line} |")
    lines += [
        "",
        "The declaration column is attributed textually: a placeholder "
        "is named for the nearest declaration header above it in the "
        "file.",
    ]
    return "\n".join(lines)
