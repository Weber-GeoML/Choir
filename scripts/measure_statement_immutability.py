r"""Corpus measurement harness for the `statement-immutability` audit.

**Not a test, and deliberately not in the default suite.** It reads
external corpora that exist only on a machine that has them (an
Isabelle2025-2 distribution, one or more lean4 elan toolchains, and for
rocq a checkout tree named by `CHOIR_ROCQ_CORPUS`), and a full run is
minutes per edit kind. Invoked by hand:

    python3 scripts/measure_statement_immutability.py \
        --corpus isabelle --kind 3a --out /tmp/isa-3a.jsonl

It changes no behaviour anywhere: it imports the real
`compare_declarations`, synthesizes the diffs a worker is *permitted* to
make (and, for the mirror measurement, ones they are not), and records
the verdict per declaration as JSONL.

`statement-immutability` blocks a merge on `CHANGED` or `UNDETERMINED`,
so the promotion question is how often either verdict lands on a diff
spec D2 permits. Four permitted edit kinds, three forbidden ones, and
`census`, which edits nothing and counts what the enumerator sees:

- `1`  fill a placeholder (`sorry`/`oops`/`undefined`/`Admitted`) in a body
- `2`  rewrite a proof body of a proof-bearing declaration (a `golf`)
- `3a` insert a permitted new helper declaration at the *earliest*
       plausible top-level position in the region between two
       consecutive declarations — the position that maximally exposes
       the trailing-trim allowlist's over-attribution
- `3b` insert the same helper immediately after the preceding
       declaration's span end — safe by construction, kept as the control
- `4a` mutate a token inside a declaration's own statement (FORBIDDEN;
       a verdict of `UNCHANGED` here is a fail-open)
- `4b` rename a declaration (FORBIDDEN; expects `CHANGED`)
- `4c` mutate a statement on a declaration line the *enumerator does
       not see* (FORBIDDEN; measures the enumeration gap directly)

For each (file, kind) **every** eligible edit goes into ONE head text and
`compare_declarations` runs once. That is sound because a declaration's
span is bounded by the *next* declaration line, so an edit inside
declaration `j`'s region can only move `j`'s own span.
`--unbatched-sample N` re-runs N randomly chosen edits one at a time and
asserts the verdicts agree, so the assumption is checked, not trusted.

An edit that could not appear in a real PR measures the harness, not the
check, so: every edit is a token-level rewrite of real corpus text or the
insertion of a two-line declaration that compiles standalone; a body is
located from the prover's own statement extractor (`stmt_region`), never
guessed; and insertion positions are restricted to lines whose first
token is a top-level command of the prover (`Corpus.plausible_toplevel`)
or a declaration line the enumerator missed — so a helper may go before
`text \<open>...\<close>`, never before `qed`.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import random
import re
import sys
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gate.inventory.scan import placeholder_regex
from gate.provers.base import ProverProfile
from gate.provers.decl_syntax import continuation_lines_for, decl_line_regex_for
from gate.provers.isabelle import ISABELLE
from gate.provers.lean4 import LEAN4
from gate.provers.rocq import ROCQ
from gate.verify.statement_equiv import (
    extract_statement,
    normalize_statement,
)
from gate.verify.statement_immutability import (
    Finding,
    _base_body_is_placeholder,
    _decl_key,
    _span_text,
    compare_declarations,
)
from gate.verify.style import (
    DeclSpan,
    comment_stripped_lines,
    find_decl_spans,
)

# --------------------------------------------------------------------------
# Corpora
# --------------------------------------------------------------------------

ISABELLE_ROOT = (
    "/opt/homebrew/Caskroom/isabelle/2025-2/Isabelle2025-2.app/src"
)
LEAN_ROOTS = sorted(
    glob.glob(os.path.expanduser("~/.elan/toolchains/*/src/lean"))
)
# A rocq toolchain ships no library source tree the way an Isabelle
# distribution or a lean4 elan toolchain does, and the 9.x split moved
# the standard library out of the compiler repo, so the rocq corpus is
# three shallow clones under one directory: `rocq/` (rocq-prover/rocq —
# `theories/` is the Corelib prelude), `stdlib/` (rocq-prover/stdlib) and
# `math-comp/`. `rocq/test-suite` is deliberately excluded: its files
# exercise error paths and are not code a worker would ever edit.
ROCQ_CORPUS = os.environ.get(
    "CHOIR_ROCQ_CORPUS", os.path.expanduser("~/rocq-corpus")
)
ROCQ_ROOTS = (
    f"{ROCQ_CORPUS}/rocq/theories",
    f"{ROCQ_CORPUS}/stdlib/theories",
    f"{ROCQ_CORPUS}/math-comp",
)

# Top-level commands that cannot be part of a preceding declaration's
# body, so a worker may legitimately insert a new declaration before one.
# Derived by frequency-ranking every line the current spans over-attribute
# (indent <= the declaration's own indent, non-blank in the comment-blanked
# copy) and hand-classifying the head of the distribution; body
# continuations (`qed`, `apply`, `next`, `where`, `termination_by`,
# `deriving`, `|`, closing brackets, prose inside string literals) are
# deliberately absent.
_LEAN4_TOPLEVEL = (
    "grind_pattern", "builtin_initialize", "initialize",
    "export", "mutual",
    "builtin_simproc", "builtin_dsimproc", "builtin_simproc_decl",
    "builtin_dsimproc_decl", "builtin_grind_propagator",
    "register_builtin_option", "register_option",
    "macro", "macro_rules", "elab", "elab_rules", "syntax",
    "declare_syntax_cat", "binder_predicate", "unif_hint",
    "recommended_spelling", "add_decl_doc", "declare_config_elab",
    "gen_injective_theorems%", "seal", "unseal",
    "infix", "infixl", "infixr", "prefix", "postfix",
    "run_cmd", "run_meta", "alias", "library_note",
    "declare_eval_bin", "declare_eval_bin_bitwise",
    "make_elab_grind_config",
    "#check", "#eval", "#print", "#guard", "#guard_msgs", "#reduce",
    "#exit", "#align",
)
_ISABELLE_TOPLEVEL = (
    "text", "txt", "chapter", "paragraph", "subparagraph",
    "subsection", "subsubsection",
    "declare", "ML", "ML_file", "ML_val", "ML_command", "SML_file",
    "instance", "instantiation", "interpretation", "sublocale",
    "subclass", "overloading", "context", "experiment",
    "translations", "no_translations", "syntax", "no_syntax",
    "type_notation", "no_type_notation", "no_notation", "nonterminal",
    "setup", "local_setup", "method_setup", "simproc_setup",
    "attribute_setup", "declaration", "syntax_declaration",
    "hide_const", "hide_type", "hide_fact", "hide_class",
    "code_datatype", "code_pred", "export_code", "code_printing",
    "code_reserved", "code_identifier", "code_module", "code_thms",
    "code_deps", "judgment",
    "value", "term", "typ", "thm", "prop", "prf", "full_prf",
    "nitpick", "refute", "quickcheck", "values", "corec",
    "corecursive", "bundle", "unbundle", "open_bundle",
    "named_theorems", "print_theorems", "print_translation",
    "parse_translation", "parse_ast_translation",
    "print_ast_translation", "typed_print_translation",
    "setup_lifting", "lifting_update", "lifting_forget", "lift_bnf",
    "free_constructors", "adhoc_overloading", "no_adhoc_overloading",
    "find_theorems", "find_consts", "unused_thms",
    "case_of_simps", "simps_of_case", "datatype_compat",
    "old_rep_datatype", "bnf", "copy_bnf", "bnf_axiomatization",
    "functor", "external_file", "compile_generated_files",
    "generate_file",
)
# Same derivation as the two tuples above, but the admissibility question
# rocq asks is narrower: a vernacular belongs here only if it cannot
# appear inside an OPEN proof. Rocq rejects a nested proof by default, so
# a helper `Lemma` inserted between two tactics would not compile, and
# `Set`/`Unset`/`Opaque`/`Transparent`/`Typeclasses`/`Time` are all legal
# mid-script — they are absent for that reason, not by oversight. So are
# `Proof using`, `Next Obligation` and `Solve Obligations`, which
# *continue* a proof rather than follow one, and the bullets
# (`-`/`+`/`*`/`{`), `with`/`where` continuations and every tactic name.
# `Import`, `Open Scope`, `Notation`, `End` and `Section` are absent for
# the opposite reason: `_NON_BODY_TRAILER_RE` already ends a span on
# them, so they are never over-attributed in the first place.
#
# `#` is the `#[...]` attribute line carrying a command after it
# (`#[local] Hint Resolve foo.`); the standalone form already ends the
# span. Matching the bare `#` is what the trailing `(?![A-Za-z0-9_'])`
# allows — the attribute's `[` is not a word character — and column-zero
# `#` is the attribute syntax and nothing else in rocq.
_ROCQ_TOPLEVEL = (
    "#",
    "Require", "From", "Export", "Include", "Module",
    "Abbreviation", "Infix", "Reserved", "Number", "Tactic",
    "Delimit", "Bind", "Declare", "Create",
    "Arguments", "Implicit", "Prenex", "Generalizable", "Context",
    "Canonical", "Coercion", "Identity", "Existing", "Register",
    "Hint", "Scheme", "Combined", "Derive", "Add",
    "Extract", "Extraction", "Ltac", "Ltac2", "Elpi", "Goal",
    "Local", "Global",
    "HB.mixin", "HB.structure", "HB.factory", "HB.builders",
    "HB.instance", "HB.end", "HB.lock", "HB.saturate",
)

# lean4 modifiers/attribute prefixes that a *declaration line* may carry.
# Superset of `gate.provers.lean4._DECL_MODIFIERS` on purpose: the extra
# entries (`public`, `meta`, `expose`) are the ones the profile does not
# know, and a line that matches this but not the real `decl_line_regex` is
# a declaration the enumerator misses.
_LEAN4_PERMISSIVE_MODIFIERS = (
    "private", "protected", "noncomputable", "partial", "unsafe",
    "nonrec", "scoped", "local", "public", "meta", "expose",
)


@dataclass(frozen=True)
class Corpus:
    """One measurable corpus: where its files are, and what a plausible
    edit looks like in its prover's syntax."""

    profile: ProverProfile
    roots: tuple[str, ...]
    suffix: str
    # Declaration kinds whose body is a *proof* — `statement_keywords`
    # minus `definition_keywords`, narrowed to the kinds that actually
    # carry an Isar/tactic proof (isabelle's `locale`/`class` land in the
    # difference but have no proof body).
    proof_bearing: tuple[str, ...]
    # The token a proof-bearing statement must end with for a body rewrite
    # to be a plausible edit. lean4's extractor stops at the first
    # paren-depth-zero `:=` or, failing that, at an equation-style `|`
    # alternative or a standalone `where` — and in those two cases the
    # "statement" runs into syntax the body owns, so replacing the body
    # deletes what made the declaration parse. isabelle needs no analogue:
    # its extractor captures a header, and the proof after it is freely
    # replaceable.
    body_terminator: str | None
    plausible_toplevel: tuple[str, ...]
    permissive_modifiers: tuple[str, ...]
    # `{n}` is the edit's index, so one file's helpers do not collide.
    helper: tuple[str, ...]
    # The body a rewrite falls back to when no other declaration in the
    # file can donate one.
    canonical_body: str
    # Delimiter pairs a synthesized edit must not separate the halves of:
    # deleting a region that closes a cartouche or a comment without
    # deleting its opener unbalances the REST OF THE FILE, the blanker then
    # reads everything after it as quoted, and the enumerator sees no
    # declarations at all. Real edits cannot do that and still compile, so
    # an unbalanced region is a harness artefact and the edit is skipped.
    balance_pairs: tuple[tuple[str, str], ...]


CORPORA: dict[str, Corpus] = {
    "isabelle": Corpus(
        profile=ISABELLE,
        roots=(ISABELLE_ROOT,),
        suffix=".thy",
        proof_bearing=("lemma", "theorem", "corollary", "proposition"),
        body_terminator=None,
        plausible_toplevel=_ISABELLE_TOPLEVEL,
        permissive_modifiers=(),
        helper=('lemma choir_aux_{n}: "True"', "  by simp"),
        canonical_body="\n  by simp",
        balance_pairs=(("\\<open>", "\\<close>"), ("(*", "*)")),
    ),
    "lean4": Corpus(
        profile=LEAN4,
        roots=tuple(LEAN_ROOTS),
        suffix=".lean",
        proof_bearing=("theorem", "lemma", "example"),
        body_terminator=":=",
        plausible_toplevel=_LEAN4_TOPLEVEL,
        permissive_modifiers=_LEAN4_PERMISSIVE_MODIFIERS,
        helper=("theorem choirAux{n} : True := by", "  trivial"),
        canonical_body=" by\n  simp_all",
        balance_pairs=(("/-", "-/"),),
    ),
    "rocq": Corpus(
        profile=ROCQ,
        roots=ROCQ_ROOTS,
        suffix=".v",
        # Exactly rocq's own `thm_token` group, which is also
        # `statement_keywords` minus `definition_keywords` with nothing
        # left over: every assertion command carries a proof, and no
        # non-assertion command does.
        proof_bearing=(
            "Theorem", "Lemma", "Corollary", "Proposition",
            "Fact", "Remark", "Property",
        ),
        # No analogue needed, for a reason neither other prover shares.
        # `extract_rocq_statement` captures one *sentence* — keyword
        # through the first `.` followed by whitespace, outside strings
        # and comments — and an assertion command's statement IS that
        # sentence: the proof lives in the sentences after it
        # (`Proof. … Qed.`), never inside the one the extractor returns.
        # So the extracted statement can never run into syntax the body
        # owns, which is the failure lean4's `:=` terminator screens for.
        body_terminator=None,
        plausible_toplevel=_ROCQ_TOPLEVEL,
        # Rocq's enumeration gap is in KEYWORDS, not modifiers:
        # `decl_modifiers` is already the complete `legacy_attr` group
        # and `decl_prefix_flags` the complete `control_flag` group, so
        # there is no unrecognized prefix to widen with. The commands the
        # scan really misses are the two-word ones (`Existing Instance`,
        # `Rewrite Rule`) and `Canonical`/`Coercion`/`Derive`/`Scheme`/
        # `Context` — a keyword set `permissive_decl_re` cannot express,
        # since it reuses `profile.decl_keywords`. Kind 4c therefore
        # measures almost nothing on rocq; that is a limit of the
        # measurement, not evidence the gap is small.
        permissive_modifiers=(),
        helper=("Lemma choir_aux_{n} : True.", "Proof. exact I. Qed."),
        canonical_body="\nProof. easy. Qed.",
        balance_pairs=(("(*", "*)"),),
    ),
}


def corpus_files(corpus: Corpus) -> list[str]:
    out: list[str] = []
    for root in corpus.roots:
        out.extend(
            sorted(glob.glob(f"{root}/**/*{corpus.suffix}", recursive=True))
        )
    return out


def permissive_decl_re(corpus: Corpus) -> re.Pattern[str]:
    """Declaration-line pattern looser than the audit's own.

    A line this matches but `decl_line_regex` does not is a declaration
    the enumerator misses. lean4 is the only corpus whose declaration
    lines carry attribute/modifier prefixes.
    """
    kws = "|".join(re.escape(k) for k in corpus.profile.decl_keywords)
    if not corpus.permissive_modifiers:
        return re.compile(rf"^\s*({kws})(?![A-Za-z0-9_'])")
    mods = "|".join(corpus.permissive_modifiers)
    return re.compile(
        rf"^\s*(?:(?:@\[[^\]]*\]|{mods})\s+)*({kws})(?![A-Za-z0-9_'])"
    )


def toplevel_re(corpus: Corpus) -> re.Pattern[str]:
    toks = "|".join(re.escape(t) for t in corpus.plausible_toplevel)
    return re.compile(rf"^\s*(?:{toks})(?![A-Za-z0-9_'])")


# --------------------------------------------------------------------------
# Shared geometry helpers
# --------------------------------------------------------------------------


def indent_of(line: str) -> int:
    return len(line) - len(line.lstrip())


def stmt_region(
    span_text: str, name: str, profile: ProverProfile
) -> tuple[int, int] | None:
    """Character offsets `(start, end)` of the extracted statement.

    The prover's own extractor decides where a statement stops, so the
    harness never has to guess where a body begins. lean4's returns a raw
    substring of its input, so `find` resolves it exactly; isabelle's
    collapses whitespace runs, so the offsets are recovered by a token
    walk — sound because it returns a whitespace-collapse of a
    *contiguous prefix* of the declaration. `None` when the extractor
    finds nothing, or when the walk does not line up (an isabelle header
    carrying a `\\<comment>` the extractor blanks out).
    """
    stmt = extract_statement(span_text, name, profile=profile)
    if stmt is None:
        return None
    at = span_text.find(stmt)
    if at != -1:
        return at, at + len(stmt)
    pos = 0
    start: int | None = None
    for tok in stmt.split():
        while pos < len(span_text) and span_text[pos].isspace():
            pos += 1
        if span_text[pos : pos + len(tok)] != tok:
            return None
        if start is None:
            start = pos
        pos += len(tok)
    if start is None:
        return None
    return start, pos


def body_region(
    span_text: str, decl_indent: int, off: int, *,
    top_re: re.Pattern[str], perm_re: re.Pattern[str],
    decl_re: re.Pattern[str],
) -> tuple[int, int]:
    """Offsets of the *plausible* proof body inside `span_text`.

    Starts where the extractor said the statement ends (`off`) and stops
    before the first line the trailing-trim allowlist over-attributed —
    a line at or shallower than the declaration's own indent whose first
    token is one of the prover's top-level commands, or a declaration
    line the enumerator missed. Without that cut a "body rewrite" would
    delete real code that merely sits inside the span (a `#check`
    command, say), and the resulting false block would be the harness's.

    Lines a real proof legitimately writes at column zero — isabelle's
    `proof`/`qed`/`apply`, lean4's `|` alternatives and `where` — are not
    in the whitelist, so they stay inside the body and isabelle's
    column-zero Isar proofs remain in scope.
    """
    pos = off
    end = len(span_text)
    while True:
        nl = span_text.find("\n", pos)
        if nl == -1:
            break
        pos = nl + 1
        eol = span_text.find("\n", pos)
        line = span_text[pos : eol if eol != -1 else len(span_text)]
        if not line.strip():
            continue
        if indent_of(line) > decl_indent:
            continue
        if top_re.match(line) or (perm_re.match(line) and not decl_re.match(line)):
            end = pos - 1
            break
    return off, max(end, off)


def delimiter_balanced(fragment: str, corpus: Corpus) -> bool:
    """True iff `fragment` opens and closes every delimiter it touches.

    See `Corpus.balance_pairs` for why an unbalanced fragment is skipped.
    Quotes are counted for every prover: both corpora treat `"` as its own
    closer.
    """
    for opener, closer in corpus.balance_pairs:
        if fragment.count(opener) != fragment.count(closer):
            return False
    return fragment.count('"') % 2 == 0


def comparison_mode(
    span: DeclSpan, blanked_base_text: str, profile: ProverProfile
) -> str:
    """Which of `compare_declarations`' three comparison modes applies."""
    if span.keyword not in profile.statement_keywords:
        return "whole-span/no-body-kind"
    if span.keyword in profile.definition_keywords:
        if _base_body_is_placeholder(
            blanked_base_text.splitlines(), span, profile=profile
        ):
            return "statement/definition-fill"
        return "whole-span/definition-body-pinned"
    return "statement/proof-bearing"


_DUP_PREFIX = "<duplicate declaration name"
_UNLOCATABLE = "<declaration text could not be located>"


def findings_by_key(
    findings: list[Finding], spans: list[DeclSpan], lines: list[str], *,
    qualified: dict[int, str], profile: ProverProfile,
) -> dict[str, Finding]:
    """Attach each `Finding` to the base declaration key that produced it.

    A name-keyed join does not work: `Finding.decl` is the *display*
    name, `qualified.get(start_line, span.name)`, while
    `compare_declarations` groups by `_decl_key`, which additionally
    carries the kind class and, for an anonymous declaration, its
    statement. So one display can stand for several keys, and — since
    the two spellings are not even the same shape — a dict keyed on
    `_decl_key` matches no finding at all: every edit reads `UNCHANGED`
    and every real verdict lands in the `collateral` bucket, a silent
    zero on the forbidden kinds.

    Position resolves most of what the name cannot.
    `compare_declarations` iterates its base index once, in
    first-appearance order of the key, and appends at most one finding
    per key, so the findings are a subsequence of that order and each
    resolves to the next key whose display it matches.

    Residual, and the reason a per-edit number wants an unbatched
    confirmation: when one display covers several keys and only some of
    them report, the leftmost match can name the wrong one of a
    same-display pair. It is a permutation within that pair — the
    blocked *count* for a file is right, the attribution is not — and
    it only arises where the display repeats at all (13% of rocq spans,
    mostly `Variables (T : Type)`, whose name token is `(T`).
    """
    order: list[tuple[str, str]] = []
    seen: set[str] = set()
    for span in spans:
        key = _decl_key(lines, span, qualified=qualified, profile=profile)
        if key in seen:
            continue
        seen.add(key)
        order.append((key, qualified.get(span.start_line, span.name)))
    out: dict[str, Finding] = {}
    at = 0
    for finding in findings:
        while at < len(order) and order[at][1] != finding.decl:
            at += 1
        if at >= len(order):
            break
        out[order[at][0]] = finding
        at += 1
    return out


def finding_verdict(finding: Finding, mode: str) -> tuple[str, str]:
    """Reconstruct `(verdict, cause)` for one `Finding`.

    `compare_declarations` returns a single file-level verdict, so a
    batched run needs the per-declaration flavour recovered from the
    `Finding`'s shape. Every branch below mirrors one branch of that
    function, in the same order.
    """
    if finding.head_statement is None:
        return "changed", "absent-from-head"
    if finding.base_statement.startswith(_DUP_PREFIX):
        return "undetermined", "duplicate-name"
    if finding.base_statement == _UNLOCATABLE:
        return "undetermined", "unlocatable-span"
    if mode.startswith("whole-span"):
        return "changed", "whole-span-text-differs"
    if finding.base_statement == "" or finding.head_statement == "":
        side = (
            "both" if finding.base_statement == "" and finding.head_statement == ""
            else "base" if finding.base_statement == "" else "head"
        )
        return "undetermined", f"extraction-failed-{side}"
    return "changed", "statement-text-differs"


# --------------------------------------------------------------------------
# Edit synthesis
# --------------------------------------------------------------------------


@dataclass
class Edit:
    """One synthesized edit, attributed to the declaration it targets."""

    decl: str
    keyword: str
    line: int
    detail: dict = field(default_factory=dict)


@dataclass
class Synth:
    blocks: list[list[str]]
    edits: list[Edit]
    skips: dict[str, int] = field(default_factory=dict)

    def head(self) -> str:
        return "\n".join(ln for block in self.blocks for ln in block)


def _skip(s: Synth, why: str) -> None:
    s.skips[why] = s.skips.get(why, 0) + 1


_LEAN_FILL_TACTIC = "simp_all"
_LEAN_FILL_TERM = "by simp"
_ISA_PROOF_FILL = "by simp"
_ISA_TERM_FILL = "0"
# `admit` stands in for one tactic, so it is replaced by one tactic;
# `Admitted`/`Abort` terminate the proof *instead of* `Qed`, so filling
# one means supplying the missing script and then closing with `Qed` —
# which is what a finished `prove` task leaves behind.
_ROCQ_TACTIC_FILL = "easy"
_ROCQ_CLOSE_FILL = "easy.\nQed"


def _fill_text(
    corpus: Corpus, token: str, span_text: str, tok_at: int
) -> str:
    """A plausible replacement for one placeholder token."""
    if corpus.profile is ISABELLE:
        return _ISA_TERM_FILL if token == "undefined" else _ISA_PROOF_FILL
    if corpus.profile is ROCQ:
        return _ROCQ_TACTIC_FILL if token == "admit" else _ROCQ_CLOSE_FILL
    before = span_text[:tok_at]
    last_assign = before.rfind(":=")
    last_by = max(
        (m.start() for m in re.finditer(r"\bby\b", before)), default=-1
    )
    tactic_position = last_by > last_assign
    return _LEAN_FILL_TACTIC if tactic_position else _LEAN_FILL_TERM


def _fill_floor(
    span_text: str, span: DeclSpan, profile: ProverProfile
) -> tuple[int, str]:
    r"""Earliest offset in `span_text` a permitted placeholder fill may touch.

    A worker fills a placeholder standing in for a *body*. One inside
    the statement — `lemma undefined_in_PiM_empty: "(\<lambda>x.
    undefined) \<in> …"`, or `axiomatization undefined :: 'a`, whose very
    NAME is the token — is no blueprint hole, and rewriting it is a
    statement edit D2 forbids, so the caller skips anything below this
    floor.

    The floor is the end of the extracted statement, except for a
    definition-bearing kind whose extractor swallows the body into the
    statement (isabelle's `where` clause): there it is the separator,
    exactly the region `_mask_definition_body` masks off when the real
    check evaluates the same fill. `(-1, "no-statement-boundary")` when
    the extractor resolves nothing; the caller then falls back to "any
    line strictly after the declaration line".
    """
    region = stmt_region(span_text, span.name, profile)
    if region is None:
        return -1, "fallback-after-decl-line"
    start, end = region
    sep = profile.definition_body_separator
    if span.keyword in profile.definition_keywords and sep is not None:
        m = re.search(rf"\b{re.escape(sep)}\b", span_text[start:end])
        if m is not None:
            return start + m.end(), "after-definition-separator"
    return end, "after-statement"


def synth_fill(
    text: str, corpus: Corpus, spans: list[DeclSpan],
    blanked: list[str], keys: dict[int, str], *, only: set[int] | None = None,
) -> Synth:
    """Kind 1: replace the first placeholder token in each declaration."""
    profile = corpus.profile
    lines = text.splitlines()
    s = Synth(blocks=[[ln] for ln in lines], edits=[])
    ph = placeholder_regex(profile.placeholder_tokens)
    for span in spans:
        if only is not None and span.start_line not in only:
            continue
        blanked_span = "\n".join(blanked[span.start_line - 1 : span.end_line])
        m = ph.search(blanked_span)
        if m is None:
            continue
        floor, floor_kind = _fill_floor(
            _span_text(lines, span) or "", span, profile
        )
        if floor < 0:
            # No statement boundary: settle for "not on the declaration
            # line", which still rules out filling the name itself.
            if blanked_span[: m.start()].count("\n") == 0:
                _skip(s, "placeholder-on-declaration-line")
                continue
        elif m.start() < floor:
            _skip(s, "placeholder-inside-statement")
            continue
        # Map the match back to (line, col) in the real file: the blanked
        # copy is aligned 1:1, so the offset splits the same way.
        upto = blanked_span[: m.start()]
        rel_line = upto.count("\n")
        col = len(upto) - (upto.rfind("\n") + 1)
        abs_line = span.start_line - 1 + rel_line
        raw = lines[abs_line]
        token = m.group(0)
        if raw[col : col + len(token)] != token:  # pragma: no cover
            _skip(s, "placeholder-misaligned")
            continue
        span_text = _span_text(lines, span) or ""
        tok_at = span_text.find(token)
        fill = _fill_text(corpus, token, span_text, max(tok_at, 0))
        new = raw[:col] + fill + raw[col + len(token) :]
        if normalize_statement(new) == normalize_statement(raw):
            _skip(s, "fill-is-noop")
            continue
        s.blocks[abs_line] = [new]
        s.edits.append(
            Edit(
                decl=keys[span.start_line], keyword=span.keyword,
                line=span.start_line,
                detail={"token": token, "fill": fill, "at_line": abs_line + 1,
                        "floor": floor_kind},
            )
        )
    return s


def synth_body_rewrite(  # noqa: PLR0915 — one linear pipeline
    text: str, corpus: Corpus, spans: list[DeclSpan],
    blanked: list[str], keys: dict[int, str], decl_re: re.Pattern[str],
    *, top_re: re.Pattern[str], perm_re: re.Pattern[str],
    only: set[int] | None = None,
) -> Synth:
    """Kind 2: replace each proof-bearing declaration's body with another's."""
    profile = corpus.profile
    lines = text.splitlines()
    s = Synth(blocks=[[ln] for ln in lines], edits=[])
    ph = placeholder_regex(profile.placeholder_tokens)
    eligible = [sp for sp in spans if sp.keyword in corpus.proof_bearing]
    bodies: list[tuple[DeclSpan, str, str, int, int]] = []
    for sp in eligible:
        span_text = _span_text(lines, sp)
        if span_text is None:  # pragma: no cover
            continue
        region = stmt_region(span_text, sp.name, profile)
        if region is None:
            _skip(s, "no-statement-boundary")
            continue
        terminator = corpus.body_terminator
        if terminator is not None and not span_text[
            region[0] : region[1]
        ].rstrip().endswith(terminator):
            _skip(s, f"statement-does-not-end-with-{terminator}")
            continue
        decl_indent = indent_of(blanked[sp.start_line - 1])
        lo, hi = body_region(
            span_text, decl_indent, region[1],
            top_re=top_re, perm_re=perm_re, decl_re=decl_re,
        )
        body = span_text[lo:hi]
        if not body.strip():
            _skip(s, "no-body-text")
            continue
        if not delimiter_balanced(body, corpus):
            _skip(s, "body-region-delimiter-unbalanced")
            continue
        bodies.append((sp, span_text, body, lo, hi))

    for i, (sp, span_text, body, lo, hi) in enumerate(bodies):
        if only is not None and sp.start_line not in only:
            continue
        donor = corpus.canonical_body
        source = "canonical"
        for j in range(1, len(bodies)):
            cand = bodies[(i + j) % len(bodies)][2]
            # A donor body carrying a placeholder token would make the
            # rewrite introduce a `sorry`, which no `golf` task does.
            if ph.search(cand) or not delimiter_balanced(cand, corpus):
                continue
            if normalize_statement(cand) != normalize_statement(body):
                donor, source = cand, "donor"
                break
        new_span = span_text[:lo] + donor + span_text[hi:]
        new_lines = new_span.split("\n")
        if any(decl_re.match(ln) for ln in new_lines[1:]):
            # Never synthesize an edit that introduces a declaration the
            # enumerator would see: a real proof body does not, and the
            # phantom would be a harness artefact, not a defect.
            if source == "donor":
                donor, source = corpus.canonical_body, "canonical"
                new_span = span_text[:lo] + donor + span_text[hi:]
                new_lines = new_span.split("\n")
            if any(decl_re.match(ln) for ln in new_lines[1:]):
                _skip(s, "would-introduce-decl-line")
                continue
        if normalize_statement(new_span) == normalize_statement(span_text):
            _skip(s, "rewrite-is-noop")
            continue
        s.blocks[sp.start_line - 1] = new_lines
        for k in range(sp.start_line, sp.end_line):
            s.blocks[k] = []
        s.edits.append(
            Edit(
                decl=keys[sp.start_line], keyword=sp.keyword,
                line=sp.start_line,
                detail={"donor": source, "body_lines": len(new_lines)},
            )
        )
    return s


def synth_helper(
    text: str, corpus: Corpus, spans: list[DeclSpan],
    blanked: list[str], keys: dict[int, str], *, position: str,
    top_re: re.Pattern[str], perm_re: re.Pattern[str],
    decl_re: re.Pattern[str], only: set[int] | None = None,
) -> Synth:
    """Kind 3: insert one new helper declaration per inter-declaration gap.

    `position="early"` picks the first *plausible* top-level line inside
    the preceding declaration's span (the position the trailing-trim
    allowlist's over-attribution can turn into a false block);
    `position="late"` picks the line immediately after that span's end
    (safe by construction, kept as the control).
    """
    lines = text.splitlines()
    s = Synth(blocks=[[ln] for ln in lines], edits=[])
    # Lines that begin inside an unterminated quoted or verbatim region
    # (`continuation_lines_for`, empty on lean4). A declaration inserted
    # there sits in a string, an `ML \<open>…\<close>` block or a
    # cartouche: it would not compile, the enumerator correctly does not
    # see it, and it gets absorbed into the preceding span — a harness
    # artefact, not a defect. Even the *safe* late position can land
    # inside an ML block, because the trailing-trim allowlist breaks on
    # SML's own `end` keyword.
    continuation = continuation_lines_for(corpus.profile, blanked)
    for i in range(len(spans) - 1):
        sp, nxt = spans[i], spans[i + 1]
        if only is not None and sp.start_line not in only:
            continue
        decl_indent = indent_of(blanked[sp.start_line - 1])
        if position == "late":
            # `sp.end_line` is 1-indexed, so it doubles as the 0-based
            # index of the first line AFTER the span — which is at or
            # before the next declaration's own line by construction.
            at = min(sp.end_line, nxt.start_line - 1)
            if at in continuation:
                _skip(s, "late-position-inside-quoted-region")
                continue
            indent = indent_of(blanked[nxt.start_line - 1])
            trigger = ""
        else:
            at = None
            for k in range(sp.start_line, sp.end_line):
                bl = blanked[k]
                if not bl.strip() or k in continuation:
                    continue
                if indent_of(bl) > decl_indent:
                    continue
                if top_re.match(bl) or (
                    perm_re.match(bl) and not decl_re.match(bl)
                ):
                    at = k
                    break
            if at is None:
                _skip(s, "no-plausible-early-position")
                continue
            indent = indent_of(blanked[at])
            trigger = lines[at].strip()[:120]
        helper = [
            indent * " " + ln.format(n=i + 1) for ln in corpus.helper
        ]
        s.blocks[at] = [*helper, ""] + s.blocks[at]
        s.edits.append(
            Edit(
                decl=keys[sp.start_line], keyword=sp.keyword,
                line=sp.start_line,
                detail={
                    "insert_at_line": at + 1,
                    "trigger": trigger,
                    "trigger_token": trigger.split()[0] if trigger else "",
                    "span_end": sp.end_line,
                    "next_decl": nxt.start_line,
                },
            )
        )
    return s


_NUMERAL_RE = re.compile(r"(?<![A-Za-z0-9_'])(\d+)(?![A-Za-z0-9_'.])")
_IDENT_RE = re.compile(r"(?<![A-Za-z0-9_'])([a-z][A-Za-z0-9_']{1,})")

# Weakenings/strengthenings a real statement edit makes, as literal
# token swaps. ASCII and isabelle-ASCII spellings both listed because
# the isabelle corpus writes `\<le>` where lean4 writes `≤`.
_SWAPS: tuple[tuple[str, str], ...] = (
    ("\\<le>", "<"), ("\\<ge>", ">"), ("\\<noteq>", "="),
    ("\\<and>", "\\<or>"), ("\\<longrightarrow>", "\\<longleftrightarrow>"),
    ("\\<forall>", "\\<exists>"), ("\\<subseteq>", "\\<subset>"),
    ("\u2264", "<"), ("\u2265", ">"), ("\u2260", "="),
    ("\u2227", "\u2228"), ("\u2200", "\u2203"), ("\u2286", "\u2282"),
    ("True", "False"), ("False", "True"),
)


def _mutate(fragment: str) -> tuple[str, str] | None:
    """A plausible statement mutation of `fragment`, or `None`.

    Three shapes, tried in order: bump a numeral (a changed bound or
    arity, the commonest real weakening); swap a relation, connective or
    quantifier for a neighbouring one (a weakened bound); prime every
    occurrence of a repeated lowercase identifier (a renamed bound
    variable). Returns `None` when the fragment carries none of them,
    and the caller records that as coverage lost rather than as a pass.
    """
    m = _NUMERAL_RE.search(fragment)
    if m is not None:
        new = str(int(m.group(1)) + 1)
        return fragment[: m.start(1)] + new + fragment[m.end(1) :], "numeral"
    for old, new in _SWAPS:
        at = fragment.find(old)
        if at == -1:
            continue
        if old.isalpha() and (
            (at and (fragment[at - 1].isalnum() or fragment[at - 1] in "_'"))
            or (
                at + len(old) < len(fragment)
                and (
                    fragment[at + len(old)].isalnum()
                    or fragment[at + len(old)] in "_'"
                )
            )
        ):
            continue
        return (
            fragment[:at] + new + fragment[at + len(old) :],
            f"operator-swap({old}->{new})",
        )
    seen: dict[str, int] = {}
    for m in _IDENT_RE.finditer(fragment):
        seen[m.group(1)] = seen.get(m.group(1), 0) + 1
    for name, count in seen.items():
        if count >= 2:
            return (
                re.sub(
                    rf"(?<![A-Za-z0-9_']){re.escape(name)}(?![A-Za-z0-9_'])",
                    name + "'",
                    fragment,
                ),
                "bound-variable-rename",
            )
    return None


def synth_statement_mutation(
    text: str, corpus: Corpus, spans: list[DeclSpan],
    keys: dict[int, str], decl_re: re.Pattern[str],
    *, only: set[int] | None = None,
) -> Synth:
    """Kind 4a: mutate a token inside each declaration's own statement."""
    lines = text.splitlines()
    s = Synth(blocks=[[ln] for ln in lines], edits=[])
    for sp in spans:
        if only is not None and sp.start_line not in only:
            continue
        span_text = _span_text(lines, sp)
        if span_text is None:  # pragma: no cover
            continue
        region = stmt_region(span_text, sp.name, corpus.profile)
        if region is None:
            _skip(s, "no-statement-boundary")
            continue
        start, end = region
        name_at = span_text.find(sp.name, start)
        window_start = (
            name_at + len(sp.name) if 0 <= name_at < end else start
        )
        if window_start >= end:
            _skip(s, "empty-statement-window")
            continue
        frag = span_text[window_start:end]
        mutated = _mutate(frag)
        if mutated is None:
            _skip(s, "no-mutable-token")
            continue
        new_span = span_text[:window_start] + mutated[0] + span_text[end:]
        new_lines = new_span.split("\n")
        if any(decl_re.match(ln) for ln in new_lines[1:]) and not any(
            decl_re.match(ln) for ln in span_text.split("\n")[1:]
        ):
            _skip(s, "would-introduce-decl-line")
            continue
        s.blocks[sp.start_line - 1] = new_lines
        for k in range(sp.start_line, sp.end_line):
            s.blocks[k] = []
        s.edits.append(
            Edit(
                decl=keys[sp.start_line], keyword=sp.keyword,
                line=sp.start_line, detail={"how": mutated[1]},
            )
        )
    return s


def synth_rename(
    text: str, spans: list[DeclSpan], keys: dict[int, str], *,
    only: set[int] | None = None,
) -> Synth:
    """Kind 4b: rename each declaration (forbidden; expects CHANGED)."""
    lines = text.splitlines()
    s = Synth(blocks=[[ln] for ln in lines], edits=[])
    for sp in spans:
        if only is not None and sp.start_line not in only:
            continue
        raw = lines[sp.start_line - 1]
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_'.]*$", sp.name):
            _skip(s, "unnameable-declaration")
            continue
        surface = sp.name.rsplit(".", 1)[-1]
        new = re.sub(
            rf"(?<![A-Za-z0-9_']){re.escape(surface)}(?![A-Za-z0-9_'])",
            surface + "_renamed",
            raw,
            count=1,
        )
        if new == raw:
            _skip(s, "rename-is-noop")
            continue
        s.blocks[sp.start_line - 1] = [new]
        s.edits.append(
            Edit(decl=keys[sp.start_line], keyword=sp.keyword,
                 line=sp.start_line, detail={})
        )
    return s


def synth_unseen_mutation(
    text: str, spans: list[DeclSpan], blanked: list[str],
    keys: dict[int, str], *,
    perm_re: re.Pattern[str], decl_re: re.Pattern[str],
) -> Synth:
    """Kind 4c: mutate a statement on a declaration line the scan misses.

    A line that a permissive declaration-line pattern matches but
    `decl_line_regex` does not is a declaration the audit never
    enumerates, so nothing pins its statement. Mutating it measures the
    enumeration gap end to end: `UNCHANGED` is a fail-open, and a
    `CHANGED`/`UNDETERMINED` verdict means the mutation was caught
    incidentally, because the line happened to sit inside a *preceding*
    declaration's over-attributed span.
    """
    lines = text.splitlines()
    s = Synth(blocks=[[ln] for ln in lines], edits=[])
    span_of: dict[int, DeclSpan] = {}
    for sp in spans:
        for k in range(sp.start_line - 1, sp.end_line):
            span_of[k] = sp
    for idx, bl in enumerate(blanked):
        if decl_re.match(bl) or not perm_re.match(bl):
            continue
        raw = lines[idx]
        mutated = _mutate(raw)
        if mutated is None:
            _skip(s, "no-mutable-token")
            continue
        if decl_re.match(mutated[0]):
            _skip(s, "mutation-would-expose-decl")
            continue
        s.blocks[idx] = [mutated[0]]
        owner = span_of.get(idx)
        s.edits.append(
            Edit(
                decl=keys[owner.start_line] if owner else "",
                keyword=owner.keyword if owner else "",
                line=owner.start_line if owner else idx + 1,
                detail={
                    "unseen_line": raw.strip()[:120],
                    "unseen_token": bl.strip().split()[0] if bl.strip() else "",
                    "inside_span": owner is not None,
                    "at_line": idx + 1,
                    "how": mutated[1],
                },
            )
        )
    return s


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

KINDS = ("census", "1", "2", "3a", "3b", "4a", "4b", "4c")


def measure_file(  # noqa: PLR0915 — one linear pipeline; splitting hurts
    path: str, corpus_name: str, kind: str, *, unbatched: int = 0,
    rng: random.Random | None = None,
) -> list[dict]:
    corpus = CORPORA[corpus_name]
    profile = corpus.profile
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except (OSError, UnicodeDecodeError) as e:
        return [{"file": path, "error": type(e).__name__}]
    decl_re = decl_line_regex_for(profile)
    top_re = toplevel_re(corpus)
    perm_re = permissive_decl_re(corpus)
    blanked = comment_stripped_lines(text, profile=profile)
    spans = find_decl_spans(text, profile=profile)
    if not spans and kind != "census":
        # Nothing to edit — but a file with ZERO enumerated declarations
        # is the most interesting census row there is, since every
        # declaration in it is invisible to the audit, so census keeps it.
        return []
    qualified = (
        profile.qualify_decl_names("\n".join(blanked))
        if profile.qualify_decl_names
        else {}
    )
    lines = text.splitlines()
    keys = {
        sp.start_line: _decl_key(lines, sp, qualified=qualified, profile=profile)
        for sp in spans
    }
    blanked_text = "\n".join(blanked)

    if kind == "census":
        # No edit at all: count what the enumerator sees and misses, so
        # the enumeration gap is reported as a population and not only
        # through the subset a mutation could be synthesized for.
        unseen: dict[str, int] = {}
        overattr: dict[str, int] = {}
        for sp in spans:
            di = indent_of(blanked[sp.start_line - 1])
            for k in range(sp.start_line, sp.end_line):
                bl = blanked[k]
                if not bl.strip() or indent_of(bl) > di:
                    continue
                tok = bl.strip().split()[0]
                if top_re.match(bl) or (
                    perm_re.match(bl) and not decl_re.match(bl)
                ):
                    overattr[tok] = overattr.get(tok, 0) + 1
        for bl in blanked:
            if perm_re.match(bl) and not decl_re.match(bl):
                tok = bl.strip().split()[0]
                unseen[tok] = unseen.get(tok, 0) + 1
        return [{
            "file": path, "kind": kind, "row": "census",
            "spans": len(spans), "lines": len(blanked),
            "unseen": unseen, "overattributed": overattr,
        }]

    if kind == "1":
        s = synth_fill(text, corpus, spans, blanked, keys)
    elif kind == "2":
        s = synth_body_rewrite(text, corpus, spans, blanked, keys,
                               decl_re, top_re=top_re, perm_re=perm_re)
    elif kind in ("3a", "3b"):
        s = synth_helper(
            text, corpus, spans, blanked, keys,
            position="early" if kind == "3a" else "late",
            top_re=top_re, perm_re=perm_re, decl_re=decl_re,
        )
    elif kind == "4a":
        s = synth_statement_mutation(text, corpus, spans, keys, decl_re)
    elif kind == "4b":
        s = synth_rename(text, spans, keys)
    elif kind == "4c":
        s = synth_unseen_mutation(text, spans, blanked, keys,
                                  perm_re=perm_re, decl_re=decl_re)
    else:  # pragma: no cover
        raise SystemExit(f"unknown kind {kind!r}")

    rows: list[dict] = []
    for why, n in s.skips.items():
        rows.append({"file": path, "kind": kind, "row": "skip",
                     "why": why, "count": n})
    if not s.edits:
        return rows

    head = s.head()
    _, findings = compare_declarations(text, head, profile=profile)
    by_decl = findings_by_key(
        findings, spans, lines, qualified=qualified, profile=profile
    )
    modes = {
        keys[sp.start_line]: comparison_mode(sp, blanked_text, profile)
        for sp in spans
    }
    edited = {e.decl for e in s.edits}
    for e in s.edits:
        f = by_decl.get(e.decl)
        mode = modes.get(e.decl, "")
        if f is None:
            rows.append({
                "file": path, "kind": kind, "row": "edit", "decl": e.decl,
                "keyword": e.keyword, "mode": mode, "blocked": False,
                "verdict": "unchanged", "cause": "", "line": e.line,
                **e.detail,
            })
        else:
            verdict, cause = finding_verdict(f, mode)
            rows.append({
                "file": path, "kind": kind, "row": "edit", "decl": e.decl,
                "keyword": e.keyword, "mode": mode, "blocked": True,
                "verdict": verdict, "cause": cause, "line": e.line,
                "base_excerpt": (f.base_statement or "")[:160],
                "head_excerpt": (f.head_statement or "")[:160]
                if f.head_statement is not None else None,
                **e.detail,
            })
    for key, f in by_decl.items():
        if key not in edited:
            mode = modes.get(key, "")
            verdict, cause = finding_verdict(f, mode)
            rows.append({
                "file": path, "kind": kind, "row": "collateral",
                "decl": key, "mode": mode, "verdict": verdict,
                "cause": cause,
            })

    # Unbatched cross-check: re-run a sample of this file's edits one at a
    # time and record whether the batched verdict agreed.
    if unbatched and rng is not None and s.edits:
        sample = rng.sample(s.edits, min(unbatched, len(s.edits)))
        for e in sample:
            solo = _solo_head(text, corpus, kind, e, top_re, perm_re,
                              decl_re, spans, blanked, keys)
            if solo is None:
                continue
            _, solo_findings = compare_declarations(text, solo, profile=profile)
            solo_f = findings_by_key(
                solo_findings, spans, lines, qualified=qualified, profile=profile
            ).get(e.decl)
            mode = modes.get(e.decl, "")
            batched = (
                finding_verdict(by_decl[e.decl], mode)
                if e.decl in by_decl else ("unchanged", "")
            )
            single = (
                finding_verdict(solo_f, mode) if solo_f is not None
                else ("unchanged", "")
            )
            rows.append({
                "file": path, "kind": kind, "row": "crosscheck",
                "decl": e.decl, "keyword": e.keyword, "mode": mode,
                "line": e.line, "agree": batched == single,
                "batched": batched[0], "single": single[0],
                "single_cause": single[1], "batched_cause": batched[0] and batched[1],
                "base_excerpt": (solo_f.base_statement or "")[:160]
                if solo_f is not None else "",
                "head_excerpt": (solo_f.head_statement or "")[:160]
                if solo_f is not None and solo_f.head_statement is not None
                else None,
                **e.detail,
            })
    return rows


def _solo_head(
    text: str, corpus: Corpus, kind: str, edit: Edit,
    top_re: re.Pattern[str], perm_re: re.Pattern[str],
    decl_re: re.Pattern[str], spans: list[DeclSpan], blanked: list[str],
    keys: dict[int, str],
) -> str | None:
    """Re-synthesize `edit` alone, for the batching cross-check.

    The full span list is still passed, so the edit is synthesized from
    the same context (kind 2's donor body is chosen from every eligible
    declaration in the file); `only` restricts which declaration the
    edit is *applied* to. That makes the solo head differ from the
    batched head in exactly one edit, which is what the cross-check
    needs to be meaningful.
    """
    only = {edit.line}
    if kind == "1":
        s = synth_fill(text, corpus, spans, blanked, keys, only=only)
    elif kind == "2":
        s = synth_body_rewrite(text, corpus, spans, blanked, keys,
                               decl_re, top_re=top_re, perm_re=perm_re,
                               only=only)
    elif kind in ("3a", "3b"):
        s = synth_helper(
            text, corpus, spans, blanked, keys,
            position="early" if kind == "3a" else "late",
            top_re=top_re, perm_re=perm_re, decl_re=decl_re, only=only,
        )
    elif kind == "4a":
        s = synth_statement_mutation(text, corpus, spans, keys, decl_re,
                                     only=only)
    elif kind == "4b":
        s = synth_rename(text, spans, keys, only=only)
    else:
        return None
    return s.head() if s.edits else None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--corpus", required=True, choices=sorted(CORPORA))
    p.add_argument("--kind", required=True, choices=KINDS)
    p.add_argument("--out", required=True)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument(
        "--sample-files", type=int, default=0,
        help="measure a random subset of this many files (for the "
             "fully-unbatched cross-check sweep)",
    )
    p.add_argument("--jobs", type=int, default=os.cpu_count() or 4)
    p.add_argument("--unbatched-sample", type=int, default=0)
    p.add_argument("--seed", type=int, default=20260822)
    args = p.parse_args(argv)

    files = corpus_files(CORPORA[args.corpus])
    if args.limit:
        files = files[: args.limit]
    if args.sample_files and args.sample_files < len(files):
        files = random.Random(args.seed).sample(files, args.sample_files)
    print(f"{args.corpus} kind={args.kind}: {len(files)} files", file=sys.stderr)

    written = 0
    with open(args.out, "w", encoding="utf-8") as out:
        if args.jobs <= 1:
            rng = random.Random(args.seed)
            for path in files:
                for row in measure_file(path, args.corpus, args.kind,
                                        unbatched=args.unbatched_sample,
                                        rng=rng):
                    out.write(json.dumps(row) + "\n")
                    written += 1
        else:
            with ProcessPoolExecutor(max_workers=args.jobs) as pool:
                futures = [
                    pool.submit(_worker, path, args.corpus, args.kind,
                                args.unbatched_sample, args.seed)
                    for path in files
                ]
                for i, fut in enumerate(futures):
                    for row in fut.result():
                        out.write(json.dumps(row) + "\n")
                        written += 1
                    if (i + 1) % 500 == 0:
                        print(f"  {i + 1}/{len(files)}", file=sys.stderr)
    print(f"wrote {written} rows to {args.out}", file=sys.stderr)
    return 0


def _worker(
    path: str, corpus_name: str, kind: str, unbatched: int, seed: int
) -> list[dict]:
    return measure_file(
        path, corpus_name, kind, unbatched=unbatched,
        rng=random.Random(f"{seed}:{path}"),
    )


if __name__ == "__main__":
    sys.exit(main())
