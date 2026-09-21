"""The rocq prover profile (design note 12 §2.2, §3.3).

`extract_rocq_statement` implements the v0 sentence-capture rule: find
the declaration by `<kw> <name>` (MULTILINE), then capture through the
first `.` that is followed by whitespace/EOL and is not inside a comment
or string — Rocq's own sentence-lexing rule, so a qualified name like
`Nat.add` is safe (its dot is followed by an identifier character, not
whitespace). Syntactic v0, same maturity as lean4's extractor;
UNDETERMINED still passes and defers to review.

`trust_report_command`/`parse_trust_report` implement the
environment-level trust report (design note 12 §4) via `Print
Assumptions`, one probe per declaration because Rocq's output does not
name the queried declaration (unlike lean4's).
"""

from __future__ import annotations

import re
from pathlib import Path

from gate.provers.base import CommentSyntax, ProverProfile, TrustEntry
from gate.provers.decl_syntax import (
    decl_line_regex_for,
    decl_prefix_fragment,
    qualify_by_scope,
)

# ---------------------------------------------------------------------------
# Statement extraction.
# ---------------------------------------------------------------------------

# Declaration keywords — module-level so `_KEYWORD` and the
# `ProverProfile.decl_keywords`/`statement_keywords` fields below share
# one source of truth (unlike lean4, every rocq decl kind has a sentence
# `extract_rocq_statement` can resolve, so `statement_keywords` is this
# same tuple, not a narrower subset).
#
# PROVENANCE: transcribed from Rocq's own machine-generated grammar,
# `doc/tools/docgram/fullGrammar` in the rocq-prover/rocq tree (fetched
# at `master`) — the file the manual's syntax blocks are generated from,
# not the manual's prose. Each group names its grammar token verbatim:
#
#   thm_token:         Theorem | Lemma | Fact | Remark | Corollary
#                      | Proposition | Property
#   def_token:         Definition | Example | SubClass
#   finite_token:      Variant | Record | Structure | Class
#   inductive_token:   Inductive | CoInductive
#   assumption_token:  Hypothesis | Variable | Axiom | Parameter
#                      | Conjecture
#   assumptions_token: Hypotheses | Variables | Axioms | Parameters
#                      | Conjectures
#
# plus, spelled out in `gallina` rather than behind a token: `Let`,
# `Fixpoint`/`CoFixpoint` (`LIST1 … SEP "with"`), `Symbol`/`Symbols`,
# `Primitive`; `Instance` from `gallina_ext`; `Function` from `command`
# (funind plugin). `Property` is why the grammar rather than the prose is
# the source: it is a real `thm_token` alternative the prose reading
# missed, so `Property p : 1 = 1.` produced ZERO spans and its statement
# could be rewritten freely on a prover with no comparator behind the
# check.
#
# STILL NOT toolchain-validated: no Rocq install exists in this
# environment (design note 12 §9 defers rocq validation to the samples
# slice), so every call in this file is grammar-grade, not execution.
#
# Deliberately NOT in this tuple, so the next reader need not re-derive
# the list:
#
# - `Let Fixpoint`/`Let CoFixpoint`, `Combined Scheme`,
#   `Identity Coercion`, `Rewrite Rule`(`s`), `Existing Instance` — all
#   two-word, and `decl_line_regex`'s
#   `^\s*(?:PREFIX\s+)*(KEYWORD)\s+(NAME)` allows one literal space
#   only. Bare `Let` still matches, so the boundary is found but the
#   enumerated name is the literal `Fixpoint`. `Rewrite Rule` is the one
#   with real trust weight (a rewrite rule is an assumption), so it is the
#   first candidate if multi-word keywords become expressible.
# - `Canonical`, `Register`(` Scheme`/` Inline`), `Coercion` — the
#   argument is an EXISTING global being given a role, not a new name.
#   (`"Canonical" OPT "Structure" global OPT [ OPT univ_decl def_body ]`,
#   so with the optional `def_body` it can define one — an under-report,
#   shared with `Coercion`.)
# - `Context`, `Derive` — `Context` declares binders with no single name;
#   `Derive`'s name is at the END of the sentence
#   (`Derive x SuchThat P As y`), out of this regex shape's reach.
# - `Scheme` — single-word, but half its spellings put a non-name in the
#   name position (`Scheme Equality for X.`), several colliding per file.
#   Declined as net-noise; revisit with a toolchain.
# - `Ltac`/`Ltac2` — not kernel objects; a retyped tactic can only break
#   its own proof, which the rebuild catches.
# - `Module`/`Module Type`/`Section` — scopes, not declarations; tracked
#   by `qualify_rocq_decl_names` below.
# - `Universe`/`Universes`/`Sort`/`Sorts`/`Constraint` — name universes
#   and sort variables, not terms with statements.
#
# Two pre-existing coverage gaps, both under- never over-reporting: a
# plural or `with`-joined declaration names several constants
# (`Axioms a b : nat.`) and only the first is enumerated; and `Instance`'s
# name is OPTIONAL (`instance_name: [ ident_decl binders | ]`), so an
# anonymous `Instance : Foo nat := …` enumerates under the literal `:`
# exactly as lean4's anonymous `instance` does — see
# `gate.verify.changed_decls._is_probeable_name` for why that is filtered
# out of probe targets but kept in enumeration.
_DECL_KEYWORDS = (
    # thm_token
    "Theorem",
    "Lemma",
    "Corollary",
    "Proposition",
    "Fact",
    "Remark",
    "Property",
    # def_token
    "Example",
    "Definition",
    "SubClass",
    # gallina: Let / Fixpoint / CoFixpoint, funind: Function
    "Let",
    "Fixpoint",
    "CoFixpoint",
    "Function",
    # gallina_ext
    "Instance",
    # inductive_token, finite_token
    "Inductive",
    "CoInductive",
    "Variant",
    "Record",
    "Structure",
    "Class",
    # assumption_token / assumptions_token
    "Axiom",
    "Axioms",
    "Parameter",
    "Parameters",
    "Conjecture",
    "Conjectures",
    "Hypothesis",
    "Hypotheses",
    "Variable",
    "Variables",
    # gallina: rewrite-rule symbols (Rocq >= 9.0) and kernel primitives
    "Symbol",
    "Symbols",
    "Primitive",
)

_KEYWORD = rf"(?:{'|'.join(_DECL_KEYWORDS)})"

# The definition-bearing subset of `statement_keywords`
# (`ProverProfile.definition_keywords`): kinds whose *body* is content,
# compared whole rather than statement-only unless the base body is still
# a placeholder. See `gate.provers.base.ProverProfile` for the
# proof-irrelevance argument.
#
# Grammar-grade only, unlike the isabelle tuple, which was verified
# against a real Isabelle2025-2 build (see `_DECL_KEYWORDS` above).
#
# The split is `thm_token` versus everything else, and that is not a
# coincidence: `thm_token` is precisely Rocq's own grouping of the
# assertion commands, i.e. the ones whose body is a proof. Everything
# else is either a definition (`def_token`, `Let`,
# `Fixpoint`/`CoFixpoint`/`Function`, `Instance`, `finite_token`,
# `inductive_token`, `Primitive`) or a bodyless assumption
# (`assumption_token`/`assumptions_token`, `Symbol`/`Symbols`) — and for
# a bodyless kind the placeholder escape can never fire, so the effect is
# simply whole-declaration comparison.
#
# Named residuals:
#
# - `Example` is the cross-prover trap: it looks like lean4's `example`
#   and behaves like the opposite. `def_token` groups it with
#   `Definition` and it takes a NAME, so it can be referenced downstream
#   and its body is content. Residual: `Example foo : 1 + 1 = 2. Proof. …
#   Qed.` is also idiomatic as a mini-theorem, and for that spelling the
#   body is a proof, so pinning costs a conservative false `CHANGED` on a
#   body rewrite. The keyword cannot distinguish the two spellings.
# - The `thm_token` family is deliberately NOT here — statement-compared,
#   so `golf` works. Residual, weaker than lean4's: lean4 *enforces* that
#   a `theorem`'s type is a `Prop`, whereas Rocq's assertion commands do
#   not, and a proof closed with `Defined` rather than `Qed` stays
#   transparent, so its body is definitionally visible downstream. Rocq's
#   proof-bearing group therefore rests on the `Qed`-opacity convention,
#   not on a kernel-enforced restriction. Not a reason to pin the whole
#   family: that would disable proof irrelevance for the overwhelmingly
#   common `Qed` case.
# - `Variable`/`Variables`/`Hypothesis`/`Hypotheses` ARE here, a
#   deliberate departure from design note 12 §3.3's "section-local
#   assumptions are deliberately *not* flagged". That decision is about
#   `trust_patterns` — whether the axiom-honesty audit should call one an
#   axiom — and stands untouched (these keywords appear in no
#   `trust_patterns` entry). Statement-immutability asks a different
#   question: closing a section discharges `Hypothesis H : P` into the
#   statement of every lemma in it, so retyping it silently changes what
#   those lemmas assert. "Not an axiom to flag" and "not a statement to
#   pin" are not the same claim.
_DEFINITION_KEYWORDS = (
    # def_token
    "Example",
    "Definition",
    "SubClass",
    # gallina: Let / Fixpoint / CoFixpoint, funind: Function
    "Let",
    "Fixpoint",
    "CoFixpoint",
    "Function",
    # gallina_ext — class fields, implicitly resolved, the same worst
    # case as lean4's `instance`
    "Instance",
    # inductive_token, finite_token — constructors and fields
    "Inductive",
    "CoInductive",
    "Variant",
    "Record",
    "Structure",
    "Class",
    # assumption_token / assumptions_token — bodyless
    "Axiom",
    "Axioms",
    "Parameter",
    "Parameters",
    "Conjecture",
    "Conjectures",
    "Hypothesis",
    "Hypotheses",
    "Variable",
    "Variables",
    # gallina — bodyless symbols, and `Primitive`, whose `:= #tag` body
    # names the kernel primitive it stands for
    "Symbol",
    "Symbols",
    "Primitive",
)

# Declaration modifiers (`ProverProfile.decl_modifiers`) — exactly Rocq's
# `legacy_attr` grammar token, the pre-`#[...]` prefix-keyword spelling
# of its attributes:
#
#   legacy_attr ::= Local | Global | Polymorphic | Monomorphic
#                  | Cumulative | NonCumulative | Private | Program
#
# and `decorated_vernac: quoted_attributes LIST0 legacy_attr vernac_aux`
# puts any of them before any command, so `Global Instance`,
# `Program Definition`, `Program Fixpoint` and `Private Inductive` all
# parse. This fragment is a shared prefix alternation across all
# keywords, not a per-keyword grammar (some of these combine with only a
# subset of `_DECL_KEYWORDS` in the real grammar) — see
# `gate.provers.decl_syntax.decl_line_regex`'s docstring for why that
# over-approximation is deliberate.
_DECL_MODIFIERS = (
    "Local",
    "Global",
    "Program",
    "Polymorphic",
    "Monomorphic",
    "Cumulative",
    "NonCumulative",
    "Private",
)

# `#[attr1, attr2]` — the modern attribute syntax `legacy_attr` is the
# pre-attribute spelling of.
_ATTRIBUTE_SYNTAX = ("#[", "]")

# Control flags (`ProverProfile.decl_prefix_flags`) — the fourth prefix
# shape. Transcribed verbatim from Rocq's own machine-generated grammar,
# `doc/tools/docgram/fullGrammar` in the rocq-prover/rocq tree (fetched
# at `master`), whose top-level entry is
#
#   vernac_control:   LIST0 control_flag decorated_vernac
#   decorated_vernac: quoted_attributes LIST0 legacy_attr vernac_aux
#   control_flag:     "Time" | "Instructions" | "Profile" OPT STRING
#                     | "Redirect" ne_string | "Timeout" natural
#                     | "AllocLimit" natural [ "Mw" | "kw" ]
#                     | "Fail" | "Succeed"
#
# so `Time Definition foo := 5.` and `Timeout 10 Theorem t : P.` are
# ordinary Rocq. Before this tuple existed each of the eight produced
# ZERO spans — the same fail-open class as lean4's `private theorem`: a
# declaration the enumerator does not see is one whose statement can be
# rewritten freely.
#
# Every flag's argument is a terminated token the grammar names exactly —
# `natural: bignat: NUMBER` (a numeral), `ne_string: STRING` (a quoted
# string), and `AllocLimit`'s unit is a bare `[ "Mw" | "kw" ]`, a
# REQUIRED choice, not the `OPT [ … ]` this grammar uses for optional
# (cf. `"Profile" OPT STRING` above). So each fragment can end on a token
# shape and nothing here uses `\S+`, per `decl_prefix_fragment`'s
# `prefix_flags` contract (raw regex fragments, no capturing groups). The
# trailing `\s+` is supplied by the caller, and it is also what makes a
# bare word self-delimiting: `Fail` cannot match the line
# `Failure Definition x := 5.`
#
# The *enumeration* half is verified by execution: all eight yield
# exactly one span with the right keyword and name, pinned by a test.
#
# Two things worth knowing rather than re-deriving:
#
# - ORDER IS NOT ENFORCED. The grammar's real order is
#   control_flag* → `#[…]` → legacy_attr* → command, and
#   `decl_prefix_fragment` flattens all three into one order-free
#   alternation — the same deliberate over-approximation
#   `decl_line_regex`'s docstring documents for the other shapes: a
#   prefix permutation that cannot occur in real Rocq costs nothing,
#   whereas failing to recognise one that can is the fail-open.
# - `Fail` ENUMERATES A PHANTOM. `Fail Definition foo := 5.` compiles
#   only if the wrapped command *fails*, so no constant `foo` exists
#   afterwards, yet the line enumerates as a declaration named `foo`.
#   Still the right trade: without the prefix the line produced no span
#   at all and was swallowed into the *preceding* declaration's span, so
#   editing it read as a change to a neighbour — and a wrong span
#   boundary is worse than a wrong name. The cost lands on name-keyed
#   consumers: `gate.indexer.extract` can flag it as a duplicate of a
#   real `foo`, and `gate.verify.statement_immutability` reports the pair
#   as an ambiguous duplicate (UNDETERMINED, which passes). `Succeed`'s
#   wrapped command does succeed; whether its effects persist is not
#   something the grammar says, so its name is at worst a phantom of the
#   same kind.
_ROCQ_NATURAL = r"[0-9][0-9_]*"
# A Rocq string literal: double-quoted, with an embedded quote written by
# doubling it. The doubled-quote branch is belt-and-braces — if that
# spelling were wrong, `Redirect "a""b"` would not be valid Rocq either.
_ROCQ_STRING = r'"[^"]*(?:""[^"]*)*"'
_CONTROL_FLAGS = (
    "Time",
    "Instructions",
    rf"Profile(?:\s+{_ROCQ_STRING})?",
    rf"Redirect\s+{_ROCQ_STRING}",
    rf"Timeout\s+{_ROCQ_NATURAL}",
    rf"AllocLimit\s+{_ROCQ_NATURAL}\s+(?:Mw|kw)",
    "Fail",
    "Succeed",
)

# Spliced into `extract_rocq_statement`'s prefix match because
# enumeration is prefix-aware: `Local Definition foo` and
# `Time Definition foo := 5.` ARE found as declarations, and an extractor
# that still assumed the keyword is the line's first token could not
# resolve the statement enumeration had just handed it — an asymmetry
# that reads as UNDETERMINED rather than as a miss.
_DECL_PREFIX = decl_prefix_fragment(
    _DECL_MODIFIERS, _ATTRIBUTE_SYNTAX, prefix_flags=_CONTROL_FLAGS
)

_COMMENT_OPEN = "(*"
_COMMENT_CLOSE = "*)"


def _scan_to_sentence_end(text: str, start: int) -> int | None:
    """Return the index just after the first sentence-ending `.`.

    A sentence-ending `.` is followed by whitespace or EOF and is not
    inside a `"…"` string or a (possibly nested) `(* … *)` comment.
    Returns `None` if no such `.` exists in the remaining text.
    """
    n = len(text)
    i = start
    in_string = False
    comment_depth = 0
    while i < n:
        if in_string:
            if text[i] == '"':
                in_string = False
            i += 1
            continue
        if comment_depth > 0:
            if text[i : i + 2] == _COMMENT_OPEN:
                comment_depth += 1
                i += 2
                continue
            if text[i : i + 2] == _COMMENT_CLOSE:
                comment_depth -= 1
                i += 2
                continue
            i += 1
            continue
        if text[i : i + 2] == _COMMENT_OPEN:
            comment_depth += 1
            i += 2
            continue
        if text[i] == '"':
            in_string = True
            i += 1
            continue
        if text[i] == ".":
            following = text[i + 1] if i + 1 < n else ""
            if following == "" or following.isspace():
                return i + 1
        i += 1
    return None


def extract_rocq_statement(text: str, decl_name: str) -> str | None:
    """Return the sentence text (keyword through the ending `.`) for `decl_name`.

    Looks for `<kw> <name>` at the start of a line (MULTILINE), then
    scans forward to the first sentence-ending `.` (dot-followed-by-blank,
    outside strings/comments). Whitespace runs are collapsed to single
    spaces and the result is stripped.

    Returns `None` if no match, or no sentence-ending `.`, is found.
    """
    prefix_re = re.compile(
        rf"(?m)^\s*{_DECL_PREFIX}{_KEYWORD}\s+{re.escape(decl_name)}\b"
    )
    match = prefix_re.search(text)
    if match is None:
        return None

    end = _scan_to_sentence_end(text, match.end())
    if end is None:
        return None

    return re.sub(r"\s+", " ", text[match.start() : end]).strip()


# ---------------------------------------------------------------------------
# Scope-path disambiguation key.
# ---------------------------------------------------------------------------

# `Module A.`, `Module Type T.`, and a functor header `Module F (X : S).`
# all open a block closed by `End <name>.` and contribute their name to
# the path. A module *definition* — `Module A := B.` — opens no block and
# has no `End`, so the trailing negative lookahead excludes it (no `:=`
# before the sentence-ending `.`); treating it as an opener would leave
# the stack permanently one deep for the rest of the file.
# `Declare Module M : S.` is bodyless too and simply does not match,
# since the line does not start with `Module`.
_MODULE_OPEN_RE = re.compile(r"^\s*Module\s+(?:Type\s+)?([A-Za-z_][\w']*)(?![^.\n]*:=)")

# `Section A.` is tracked but contributes NOTHING to the path, as lean4's
# `section` is: a Rocq `Section` does not qualify the declarations inside
# it (it generalizes them over the section's variables), and two
# same-named declarations in two sections of one module is not legal Rocq
# anyway. Tracking is still necessary so its `End A.` does not pop an
# enclosing `Module`.
_SECTION_OPEN_RE = re.compile(r"^\s*Section\s+([A-Za-z_][\w']*)")

_END_RE = re.compile(r"^\s*End\s+([A-Za-z_][\w']*)\s*\.")


def qualify_rocq_decl_names(text: str) -> dict[int, str]:
    """Enclosing-`Module` path per declaration line — a DISAMBIGUATION key.

    Not name resolution: see `gate.provers.decl_syntax.qualify_by_scope`
    for why that distinction is what makes this implementable at all
    without a Rocq toolchain, and for the failure modes it bounds. Rocq
    really does qualify a declaration inside `Module A` as `A.c`, so this
    key often coincides with the resolved name — but it ignores
    `Import`/`Export`/`Include` and functor application, so it must not
    be relied on as one.

    What it fixes: two `Theorem c`s in sibling `Module`s used to group
    under the bare surface name `c`, so filling one's proof made the
    per-name text multiset differ while the name was unique on neither
    side — the ambiguous-duplicate branch fired on an ordinary, entirely
    permitted edit and reported `UNDETERMINED`.
    """
    return qualify_by_scope(
        text,
        decl_re=_DECL_LINE_RE,
        qualifying_open_re=_MODULE_OPEN_RE,
        plain_open_re=_SECTION_OPEN_RE,
        close_re=_END_RE,
    )


# ---------------------------------------------------------------------------
# Trust-report hooks (design note 12 §4).
#
# `Print Assumptions <decl>.` prints either `Closed under the global
# context` (no axioms) or an `Axioms:` header followed by one
# `name : type` line per axiom — but it never echoes the queried
# declaration's own name. Rather than thread a marker through the
# compiler's output, each probe covers exactly one target
# (`one_probe_per_decl = True` on `ROCQ`); `collect_trust_report` invokes
# the builder once per decl and attributes the decl-less parsed result to
# whichever target produced it.
#
# `rocq c FILE.v` is the ≥9.0 unified CLI's compile subcommand. An
# installation shipping only the legacy `coqc` binary should swap the
# first argv element — same single-file compile semantics, same stdout
# shape.
#
# Not run against a live toolchain (design note 12 §9 defers Rocq
# validation to the samples slice); the parser is unit-tested against
# canned transcripts matching Rocq's documented output.
# ---------------------------------------------------------------------------

_TRUST_PROBE_FILENAME = ".choir-trust-probe.v"

_AXIOMS_HEADER = "Axioms:"
_CLOSED_CONTEXT = "Closed under the global context"


def build_rocq_trust_probe(
    workspace: Path, targets: list[str], imports: list[str]
) -> Path:
    """Write `<workspace>/.choir-trust-probe.v` and return its path.

    One `Require Import <module>.` line per `imports` entry (in
    order), then one `Print Assumptions <decl>.` line per target (in
    order — in practice always a single target, per
    `one_probe_per_decl`).
    """
    lines = [f"Require Import {module}." for module in imports]
    lines.extend(f"Print Assumptions {decl}." for decl in targets)
    probe_path = workspace / _TRUST_PROBE_FILENAME
    probe_path.write_text("\n".join(lines) + "\n")
    return probe_path


def rocq_trust_report_command(
    workspace: Path, targets: list[str], imports: list[str]
) -> list[str]:
    """Write the trust probe and return the `rocq c` invocation."""
    build_rocq_trust_probe(workspace, targets, imports)
    return ["rocq", "c", _TRUST_PROBE_FILENAME]


def parse_rocq_trust_report(output: str) -> list[TrustEntry]:
    """Parse a single-decl `Print Assumptions` transcript.

    Rocq's own output never names the declaration that was queried, so
    this returns at most one entry with `decl=""` as a placeholder —
    the caller (a single-decl probe run via `one_probe_per_decl`) is
    the one that knows which target produced this output, and fills
    `decl` in afterward (`gate.provers.trust.collect_trust_report`).
    """
    lines = output.splitlines()
    for i, raw_line in enumerate(lines):
        line = raw_line.strip()
        if line == _CLOSED_CONTEXT:
            return [TrustEntry(decl="", assumptions=(), clean=True)]
        if line == _AXIOMS_HEADER:
            assumptions: list[str] = []
            for cont_line in lines[i + 1 :]:
                cont_stripped = cont_line.strip()
                if not cont_stripped:
                    break
                name = cont_stripped.split(" : ", 1)[0].strip()
                assumptions.append(name)
            return [TrustEntry(decl="", assumptions=tuple(assumptions), clean=False)]
    return []


# ---------------------------------------------------------------------------
# The profile
# ---------------------------------------------------------------------------

ROCQ = ProverProfile(
    name="rocq",
    file_extensions=(".v",),
    comment_syntax=CommentSyntax(line=None, block_open="(*", block_close="*)"),
    decl_keywords=_DECL_KEYWORDS,
    statement_keywords=_DECL_KEYWORDS,
    definition_keywords=_DEFINITION_KEYWORDS,
    placeholder_tokens=("Admitted", "admit", "Abort"),
    trust_patterns=(
        ("axiom", r"^\s*(?:Axiom|Axioms|Parameter|Parameters|Conjecture)\b"),
        ("admitted", r"^\s*Admitted\."),
        ("native_compute", r"\bnative_compute\b"),
        ("vm_compute", r"\bvm_compute\b"),
        ("universe_checking", r"^\s*Unset\s+Universe\s+Checking\b"),
        ("ml_module", r"^\s*Declare\s+ML\s+Module\b"),
    ),
    build_command=("dune", "build"),
    toolchain_file=None,
    protected_files=("_CoqProject", "dune-project", "*.opam"),
    extra_audits=(),
    search_tooling_note=False,
    extract_statement=extract_rocq_statement,
    trust_report_command=rocq_trust_report_command,
    parse_trust_report=parse_rocq_trust_report,
    one_probe_per_decl=True,
    decl_modifiers=_DECL_MODIFIERS,
    attribute_syntax=_ATTRIBUTE_SYNTAX,
    decl_prefix_flags=_CONTROL_FLAGS,
    qualify_decl_names=qualify_rocq_decl_names,
)

# `qualify_rocq_decl_names`'s declaration-boundary regex. Read off the
# profile so every prefix shape stays in step with the other `gate/`
# scanners — a hand-written argument list here is the drift
# `decl_line_regex_for` exists to prevent. Defined after `ROCQ` because
# it reads it; the only use is inside `qualify_rocq_decl_names`, so the
# forward reference resolves.
_DECL_LINE_RE = decl_line_regex_for(ROCQ)
