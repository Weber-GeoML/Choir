"""Statement-immutability audit (spec D2).

D2: the orchestrator authors every theorem statement; a worker's PR is
a proof-body fill, never a statement edit. This module is the pure
comparison logic — every declaration present in the base version of a
changed file must reach head with its statement untouched. The PR
wrapper is `statement_immutability_cli.py`.

**Text first, parser second**, per declaration name in base:

- absent from head -> `CHANGED` (deleting a statement is changing it,
  unambiguous however many base declarations shared the name);
- base's and head's multisets of normalized span texts are equal ->
  `UNCHANGED`, with no parsing at all;
- they differ and the name is not unique on both sides ->
  `UNDETERMINED` (something moved among same-named declarations and
  there is no way to say which);
- they differ and the name is unique -> the kind decides how (see the
  three modes below);
- a name present only in head is ignored — new helper declarations are
  what D2 leaves to the worker.

Order matters: an unchanged text means nothing about that declaration
changed, so no extraction is needed to know it. Asking the parser first
made every shape the parser could not handle fail *in a file compared
against itself* (equation-style `def` with no top-level `:=`; two
anonymous `example`s colliding on the literal `:` token), which is why
this check could not block. Text-first narrows `UNDETERMINED` to one
meaning — *this declaration's text changed and I cannot tell whether
the statement moved* — which is rare, genuinely suspicious, and worth a
hard stop.

**`UNDETERMINED` FAILS here, the inverse of `statement_equiv`**, where
an unparseable declaration passes. The two answer different questions:
"does this proof match the target statement" (best-effort, defers on
doubt) versus "did you touch anything you weren't supposed to" — and
"I can't tell" answers the second with a block.

**Three comparison modes, chosen from the base span's keyword once the
text already differs.** `profile.statement_keywords` names the kinds
with a statement/body split; `profile.definition_keywords` splits those
in two:

- outside `statement_keywords` (lean4 `structure`/`class`/`inductive`/
  `axiom`/`opaque`): no body a change could legitimately live in, so
  any text difference is `CHANGED`. This is what catches a worker
  silently redefining a shared `structure` or `inductive`.
- proof-bearing (`statement_keywords` minus `definition_keywords`):
  the body is kernel-checked, so any valid proof serves and a `golf`
  task exists to rewrite it. Statement comparison only.
- definition-bearing: the body IS the content, so the whole
  declaration is compared — except when the base body still carries a
  `profile.placeholder_tokens` token, which is a blueprint node
  awaiting its fill, and then the statement alone is compared.

The placeholder escape recognises `placeholder_tokens` and nothing
else. Named consequence: an isabelle blueprint definition body reports
`CHANGED` when filled, accepted because the alternative was an
invisible body rewrite. Isabelle also needs `definition_body_separator`
for the escape to work at all — `extract_isabelle_statement` captures
the header *including* its `where` clause, so the extracted "statement"
contains the body and a legitimate fill would read as `CHANGED`;
`_mask_definition_body` truncates both sides so the signature alone is
compared.

**Normalization** reuses `statement_equiv.normalize_statement`
(collapse whitespace runs) on whole span texts. Whitespace carries no
trust information, so a reflow is `UNCHANGED`; the tolerance stops at
the token boundary, so an alpha-renamed binder stays `CHANGED` —
under D2 a worker touches no statement at all. Whether the reflowed
file still compiles is the rebuild's question.

**A duplicate-name finding reports each side's count, never one
match** — any single same-named span is an arbitrary pick, and the
orchestrator reads these findings to triage.

**Grouping is by namespace-qualified name** where the profile supplies
`qualify_decl_names`, so `A.comm` and `B.comm` stop colliding into one
ambiguous bucket. On lean4 that hook is real qualification; **on
isabelle and rocq it is a disambiguation key, not name resolution** — a
correct qualifier there would need locale interpretation and functor
application, and is not needed, because the key only matches base
against head within one file. The ambiguous branch stays reachable for
two declarations sharing a name *and* a scope, and for two anonymous
`example`s.

Because the grouping key and the name an extractor can find are no
longer the same string, both extraction sites pass the span's own
**surface** name and keep the qualified key for grouping and reporting;
extraction is scoped to one span's text, never the whole file, since
lean4's extractor also matches a qualified name's last segment.

`Verdict`/`Finding` are this module's own types; the success member is
`UNCHANGED`, not `EQUIVALENT`, because the question is "untouched?".
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from enum import Enum

from gate.inventory.scan import placeholder_regex
from gate.provers.base import ProverProfile
from gate.verify.statement_equiv import extract_statement, normalize_statement
from gate.verify.style import DeclSpan, comment_stripped_lines, find_decl_spans


class Verdict(Enum):
    UNCHANGED = "unchanged"
    CHANGED = "changed"
    UNDETERMINED = "undetermined"


@dataclass(frozen=True)
class Finding:
    """One base declaration whose statement immutability could not be confirmed.

    `head_statement` is `None` exactly when the declaration is absent
    from head — deleted, or renamed to a different declaration name
    (the check is name-keyed, so it cannot tell the two apart; either
    way D2 forbids a worker doing it unilaterally). When the
    declaration is present but its statement could not be extracted on
    either side, the unavailable side is reported as an empty string
    rather than `None` — so `None` always means "absent," never
    "present but unparseable."

    For a name that is not unique on a side, the corresponding field
    carries a description of the ambiguity (with that side's count)
    rather than any declaration text — see `_duplicate_name_note` and
    the module docstring.
    """

    decl: str
    base_statement: str
    head_statement: str | None


@dataclass(frozen=True)
class _Indexed:
    """One declaration of a side, with what the comparison needs of it.

    `text` is the span's normalized text, or `None` for a span whose
    text could not be located at all (see `_span_text`). `display` is
    the name a report should show: the grouping key carries a kind-class
    prefix and, for anonymous declarations, the statement, none of which
    belongs in a report.
    """

    span: DeclSpan
    text: str | None
    display: str


@dataclass(frozen=True)
class _SideIndex:
    """One side's declarations grouped by `_decl_key`, in file order.

    A key's entries are that key's count on this side, and its single
    entry is the span to compare when the key is unique. `blanked_lines`
    is the side's comment-blanked text, kept because both the
    qualification hook and `_base_body_is_placeholder` need it and
    blanking is a per-character pass over the whole file.
    """

    decls: dict[str, list[_Indexed]]
    lines: list[str]
    blanked_lines: list[str]


def _duplicate_name_note(count: int, side: str, *, detail: str) -> str:
    """Describe a non-unique name on one side, quoting no declaration.

    Any single same-named match would be an arbitrary pick, so the count
    is what is reported. Report-only; no verdict depends on this text.
    `detail` is the caller's, because the two callers are true of
    different things: in the ambiguous case something moved and this
    module cannot say which, in the deletion case every one of them is
    gone and nothing is ambiguous.
    """
    return f"<duplicate declaration name: {count} in {side} — {detail}>"


_AMBIGUOUS_DETAIL = (
    "the text changed among them, so which one moved cannot be determined; "
    "no single declaration is quoted, since any one match would be arbitrary"
)

_DELETED_DETAIL = (
    "no single declaration is quoted, since any one match would be arbitrary"
)


# Reported when a span's text could not be located at all — defensive;
# unreachable from this module's own callers (see `_span_text`).
_UNLOCATABLE_NOTE = "<declaration text could not be located>"


def _span_text(lines: list[str], span: DeclSpan) -> str | None:
    """Return the text `span` covers in `lines`, or `None` if unlocatable.

    `find_decl_spans` always returns spans within range of the text it
    just scanned, so `None` here is defensive — kept so a span this
    module can't locate fails safe to `UNDETERMINED` rather than (worse,
    under text-first comparison) comparing equal to another unlocatable
    span and reading as `UNCHANGED`.

    Takes the already-split lines: callers hold a whole file and one
    span per declaration, so splitting per call is quadratic.
    """
    if span.start_line < 1 or span.end_line > len(lines) or span.start_line > span.end_line:
        return None
    return "\n".join(lines[span.start_line - 1 : span.end_line])


def _mask_definition_body(statement: str, profile: ProverProfile) -> str:
    """Truncate `statement` at the prover's definition-body separator.

    Only reached for a definition-bearing declaration whose base body was
    a placeholder — the permitted blueprint fill. Needed because
    `extract_isabelle_statement` captures the header *including* its
    `where` clause, so the "statement" of `definition foo :: nat where
    "foo = sorry"` contains the body and a legitimate fill would read as
    `CHANGED`. Truncating at the separator compares the signature.

    A profile with no separator (lean4, rocq — their extractors already
    stop before the body) gets `statement` back unchanged, as does a
    statement the separator does not appear in.
    """
    separator = profile.definition_body_separator
    if separator is None:
        return statement
    match = re.search(rf"\b{re.escape(separator)}\b", statement)
    if match is None:
        return statement
    return statement[: match.start()]


def _base_body_is_placeholder(
    blanked_base_lines: list[str], span: DeclSpan, *, profile: ProverProfile
) -> bool:
    """True iff the base declaration at `span` was published unfilled.

    i.e. may a worker rewrite this definition-bearing declaration's body?
    Yes exactly when base still carries a placeholder (`def foo : Nat :=
    sorry` awaiting a fill), no when the body was already real content.

    Reads the **comment-blanked** base lines, so a declaration whose body
    is real content but which carries a `-- TODO: sorry` comment does not
    thereby license rewriting that body.

    Asks "does this declaration carry a placeholder token anywhere in its
    span" rather than "in the region after its statement": slicing the
    body off precisely would mean locating the extracted statement inside
    the span text, which the isabelle and rocq extractors make impossible
    (both collapse whitespace runs, so what they return is not a
    substring of the span). The two readings differ only for a
    placeholder inside a *type* — and such a declaration is unfilled
    anyway.
    """
    span_text = _span_text(blanked_base_lines, span)
    if span_text is None:
        return False
    return placeholder_regex(profile.placeholder_tokens).search(span_text) is not None


def _extract_for(span_text: str, span: DeclSpan, *, profile: ProverProfile) -> str | None:
    """Extract `span`'s statement, retrying anonymously if its name is not one.

    The surface name is tried first, so every named declaration — and
    lean4's anonymous `example`, whose captured `:` its own extractor
    resolves — behaves exactly as before. Only when that fails *and* the
    token is not name-shaped do we ask again with the empty name, which
    the profile contract defines as "anchor on the keyword instead."

    A fallback rather than a switch: an anonymous declaration is
    identified by extraction failing on a non-name, not by deciding up
    front which spellings are anonymous on which prover.
    """
    statement = profile.extract_statement(span_text, span.name)
    if statement is None and _is_nameless(span.name, profile=profile):
        statement = profile.extract_statement(span_text, "")
    return statement


def _kind_class(span: DeclSpan, *, profile: ProverProfile) -> str:
    """Which of the three comparison classes `span`'s keyword falls in.

    A kind outside `statement_keywords` has no proof body and is compared
    whole; a kind in `definition_keywords` has a body that *is* content;
    anything else is proof-bearing and only its statement is compared.
    The single source for both the grouping key and the comparison, so
    the two cannot disagree about a declaration's class.
    """
    if span.keyword not in profile.statement_keywords:
        return "whole"
    if span.keyword in profile.definition_keywords:
        return "defn"
    return "proof"


_IDENTIFIER_RE = re.compile(r"^[A-Za-z_\\][A-Za-z0-9_.'\\<>^]*$")


def _is_nameless(name: str, *, profile: ProverProfile) -> bool:
    """True when `name` is not something the prover would call a name.

    An anonymous declaration has no name to capture, so the span scanner
    reports whatever token followed the keyword: `:` for a lean4
    `example`, and on isabelle a *clause* keyword — `assumes`, `fixes`,
    `shows` — when a long-goal command's own line ends after `lemma`.
    Treating those as names collides two anonymous declarations on one
    key, which reports `UNDETERMINED` for a permitted proof fill.

    `reserved_non_names` covers the name-shaped ones (`assumes` after a
    bare `lemma` line opens a clause), which shape alone cannot see.
    """
    return name in profile.reserved_non_names or not bool(_IDENTIFIER_RE.match(name))


def _decl_key(
    lines: list[str],
    span: DeclSpan,
    *,
    qualified: dict[int, str],
    profile: ProverProfile,
) -> str:
    """Return the grouping key for `span`.

    Three components:

    **The kind class**, because `definition convex` and `lemma convex`
    are different declarations that happen to share a surface name
    (`HOL/Analysis/Convex.thy` has exactly that pair). Two declarations
    of the *same* class sharing a name in one scope still collide,
    correctly — that is the ambiguous case `UNDETERMINED` exists for.

    **The qualified name**, when `qualified` covers this span's start
    line; the raw surface name otherwise (a profile with no hook, or a
    line the hook has no entry for — defensive, since both are built
    from the same declaration regex).

    **The statement, for anonymous declarations only**, whose statement
    *is* their identity. Keying on it makes filling one's proof a match
    while changing one's statement reads as a delete plus an add, which
    still blocks. If extraction fails the raw token stands and the
    collision remains.
    """
    name = qualified.get(span.start_line, span.name)
    if _is_nameless(span.name, profile=profile):
        statement = _extract_for(_span_text(lines, span) or "", span, profile=profile)
        if statement is not None:
            name = f"<anon>{normalize_statement(statement)}"
    return f"{_kind_class(span, profile=profile)}\x00{name}"


def _index_side(text: str, *, profile: ProverProfile) -> _SideIndex:
    """Enumerate one side's declarations, grouped by `_decl_key`.

    One `find_decl_spans` pass plus one `_span_text`/`normalize_statement`
    per span. No statement extraction happens here — the parser is not
    consulted until a name's text is known to have changed.

    The qualification hook is fed the **comment-blanked** copy of the
    text (aligned 1:1 with the real lines, so the line numbers it returns
    still index the real file), for the same reason `find_decl_spans`
    matches declarations there: `qualify_decl_names` tracks
    `namespace`/`section`/`end` on a text-level stack, so an opener or a
    bare `end` inside a comment would qualify every later declaration
    under a namespace that does not exist. (`gate.verify.changed_decls`
    calls the same hook on raw text and has the same latent issue.)
    """
    lines = text.splitlines()
    blanked_lines = comment_stripped_lines(text, profile=profile)
    qualified = (
        profile.qualify_decl_names("\n".join(blanked_lines))
        if profile.qualify_decl_names
        else {}
    )
    decls: dict[str, list[_Indexed]] = {}
    for span in find_decl_spans(text, profile=profile):
        key = _decl_key(lines, span, qualified=qualified, profile=profile)
        raw = _span_text(lines, span)
        decls.setdefault(key, []).append(
            _Indexed(
                span=span,
                text=raw if raw is None else normalize_statement(raw),
                display=qualified.get(span.start_line, span.name),
            )
        )
    return _SideIndex(decls=decls, lines=lines, blanked_lines=blanked_lines)


def _base_report_text(
    lines: list[str], entries: list[_Indexed], *, profile: ProverProfile
) -> str:
    """Best available rendering of a deleted base declaration, for a `Finding`.

    Report-only — the caller has already decided the verdict, so the
    extraction below cannot turn a resolved comparison into an
    unresolved one. A statement reads better in a report than a whole
    span with its proof body attached.

    A non-unique name renders as `_duplicate_name_note` (there is no
    single declaration to quote); an unresolvable side renders as `""`,
    which `Finding` documents as "present, but unparseable".

    Extraction is scoped to this span's own text and asks for the span's
    own **surface** name: a whole-file search can resolve a
    qualified-or-last-segment name to the wrong same-surface-named
    declaration elsewhere in the file, and the isabelle and rocq
    extractors have no last-segment fallback at all, so a qualified key
    like `A.c` finds nothing in a span reading `Theorem c : …`.
    """
    if len(entries) > 1:
        return _duplicate_name_note(len(entries), "base", detail=_DELETED_DETAIL)
    span = entries[0].span
    span_text = _span_text(lines, span)
    if _kind_class(span, profile=profile) == "whole":
        rendered = span_text
    else:
        rendered = (
            extract_statement(span_text, span.name, profile=profile)
            if span_text is not None
            else None
        )
    return rendered if rendered is not None else ""


def count_base_declarations(text: str, *, profile: ProverProfile) -> int:
    """How many declarations `compare_declarations` enumerates in `text`.

    Lets a caller tell "checked, and every base declaration is untouched"
    from "there was nothing here to check" — `compare_declarations`
    returns `UNCHANGED` for both, correctly, but a report that renders
    them identically claims a file was verified when it was only skipped.
    A base version with no enumerable declarations is the ordinary case
    for a brand-new file, and also what a prover-specific enumeration gap
    looks like from outside.
    """
    return len(find_decl_spans(text, profile=profile))


def compare_declarations(
    base_text: str, head_text: str, *, profile: ProverProfile
) -> tuple[Verdict, list[Finding]]:
    """Compare every base declaration against head, text first.

    See the module docstring for the semantics, why that order is the
    design rather than an optimization, and what `UNDETERMINED` means.

    Presence/absence is decided from span counts, never from whether a
    text or statement resolved — a deleted declaration and a
    present-but-unresolvable one are different verdicts (`CHANGED` vs
    `UNDETERMINED`) and only the count tells them apart.

    The comparison mode comes from the *base* span's keyword. A
    declaration is not expected to change kind; if it somehow does, the
    text differs and the base side's mode still reports a real
    difference, which is the safe outcome.
    """
    base = _index_side(base_text, profile=profile)
    head = _index_side(head_text, profile=profile)

    findings: list[Finding] = []
    saw_undetermined = False
    saw_changed = False

    for key, base_entries in base.decls.items():
        # `key` groups; `display` is what a report says.
        name = base_entries[0].display
        head_entries = head.decls.get(key, [])

        # Absent from head: deleting a statement is changing it, and it
        # is unambiguous however many base declarations shared the name.
        if not head_entries:
            findings.append(
                Finding(
                    decl=name,
                    base_statement=_base_report_text(
                        base.lines, base_entries, profile=profile
                    ),
                    head_statement=None,
                )
            )
            saw_changed = True
            continue

        # Checked *before* the multiset comparison so it cannot fail
        # open: two unlocatable spans would otherwise compare equal and
        # read as UNCHANGED. Defensive — see `_span_text`.
        if any(e.text is None for e in (*base_entries, *head_entries)):
            findings.append(
                Finding(
                    decl=name,
                    base_statement=_UNLOCATABLE_NOTE,
                    head_statement=_UNLOCATABLE_NOTE,
                )
            )
            saw_undetermined = True
            continue

        # The load-bearing shortcut: identical text means nothing about
        # this name changed, whatever the parser would have made of it.
        if Counter(e.text for e in base_entries) == Counter(
            e.text for e in head_entries
        ):
            continue

        # The text changed and the name repeats, so "did *this*
        # statement move?" has no answer.
        if len(base_entries) > 1 or len(head_entries) > 1:
            findings.append(
                Finding(
                    decl=name,
                    base_statement=_duplicate_name_note(
                        len(base_entries), "base", detail=_AMBIGUOUS_DETAIL
                    ),
                    head_statement=_duplicate_name_note(
                        len(head_entries), "head", detail=_AMBIGUOUS_DETAIL
                    ),
                )
            )
            saw_undetermined = True
            continue

        # Unique on both sides, and the text differs. Only now does it
        # matter whether the difference could legitimately be a proof body.
        base_span = base_entries[0].span
        head_span = head_entries[0].span
        base_span_text = _span_text(base.lines, base_span)
        head_span_text = _span_text(head.lines, head_span)
        kind = _kind_class(base_span, profile=profile)
        # A definition-bearing kind the base version published unfilled:
        # the permitted blueprint fill. Needed below as well as here —
        # on isabelle the extracted statement contains the body, so the
        # fill has to be compared with the body masked off.
        filling_blueprint = kind == "defn" and _base_body_is_placeholder(
            base.blanked_lines, base_span, profile=profile
        )
        # Whole-declaration comparison, for two reasons with one outcome:
        # a `whole` kind has no statement/body split, so the span *is*
        # the pinned text; a filled `defn` body is content the worker may
        # not rewrite, and comparing only the statement would report
        # `def foo : Nat := 5` becoming `:= 6` as UNCHANGED. Neither
        # consults the extractor — the text difference is already
        # established and no difference is permitted — so a declaration
        # the extractor cannot parse still gets a real verdict here.
        if kind == "whole" or (kind == "defn" and not filling_blueprint):
            findings.append(
                Finding(
                    decl=name,
                    base_statement=base_span_text if base_span_text is not None else "",
                    head_statement=head_span_text if head_span_text is not None else "",
                )
            )
            saw_changed = True
            continue

        base_stmt = (
            _extract_for(base_span_text, base_span, profile=profile)
            if base_span_text is not None
            else None
        )
        head_stmt = (
            _extract_for(head_span_text, head_span, profile=profile)
            if head_span_text is not None
            else None
        )

        if base_stmt is None or head_stmt is None:
            findings.append(
                Finding(
                    decl=name,
                    base_statement=base_stmt if base_stmt is not None else "",
                    head_statement=head_stmt if head_stmt is not None else "",
                )
            )
            saw_undetermined = True
            continue

        # A permitted blueprint fill is compared on the signature only.
        # The `Finding` below still quotes the untruncated statements, so
        # a report never hides what actually differs.
        base_cmp = base_stmt
        head_cmp = head_stmt
        if filling_blueprint:
            base_cmp = _mask_definition_body(base_stmt, profile)
            head_cmp = _mask_definition_body(head_stmt, profile)

        if normalize_statement(base_cmp) != normalize_statement(head_cmp):
            findings.append(
                Finding(decl=name, base_statement=base_stmt, head_statement=head_stmt)
            )
            saw_changed = True
        # else: the difference was confined to the proof body — exactly
        # what D2 permits, so no finding.

    if saw_undetermined:
        return Verdict.UNDETERMINED, findings
    if saw_changed:
        return Verdict.CHANGED, findings
    return Verdict.UNCHANGED, []
