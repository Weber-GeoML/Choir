"""The lean4 prover profile — the reference profile (design note 12 §3.1).

`extract_lean_statement` scans from the declaration keyword to whichever
of `declVal`'s three shapes (`declValSimple <|> declValEqns <|>
whereStructInst`, per `Lean/Parser/Command.lean`) ends the signature;
`gate/verify/statement_equiv.py` delegates to it.

`trust_report_command`/`parse_trust_report` implement the
environment-level trust report (design note 12 §4): a `#print axioms`
probe run via `lake env lean`.

`qualify_decl_names` computes the name the prover actually resolves, so
it is the one fed to trust-report probe targets. That is why it stays
separate from the shared `gate.provers.decl_syntax.qualify_by_scope`,
which computes only a stable disambiguation key — see its docstring.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from gate.provers.base import (
    CommentSyntax,
    DeclDependency,
    ProverProfile,
    TrustEntry,
)
from gate.provers.decl_syntax import (
    decl_line_regex_for,
    decl_name_from,
    decl_prefix_fragment,
)

# Declaration keywords — module-level so `qualify_decl_names` and the
# `ProverProfile.decl_keywords` field below share one source of truth.
# `opaque` belongs here, not in `_DECL_MODIFIERS`: Lean's own
# `declaration` production lists it as a decl-keyword *alternative*
# alongside `theorem`/`def`/`instance`. It has no statement/body split,
# so it stays out of `_STATEMENT_KEYWORDS`.
_DECL_KEYWORDS = (
    "theorem",
    "lemma",
    "def",
    "abbrev",
    "instance",
    "example",
    "structure",
    "class",
    "inductive",
    "axiom",
    "opaque",
)

# ---------------------------------------------------------------------------
# Statement extraction — copied from gate/verify/statement_equiv.py.
# ---------------------------------------------------------------------------

# Decl keywords whose kind has a statement/proof-body split that
# `extract_lean_statement` can resolve. The other five (`structure`,
# `class`, `inductive`, `axiom`, `opaque`) have none — a `structure`'s
# "statement" is its whole declaration. `_KEYWORD`'s alternation is
# built from this tuple so the two cannot drift apart;
# `gate.verify.statement_immutability` (spec D2) reads it via
# `ProverProfile.statement_keywords` to decide, per declaration, whether
# to compare just the statement or the whole span.
_STATEMENT_KEYWORDS = ("theorem", "lemma", "def", "abbrev", "instance", "example")

# The definition-bearing half of `_STATEMENT_KEYWORDS`
# (`ProverProfile.definition_keywords`): kinds whose *body* is content
# rather than a proof, so `gate.verify.statement_immutability` compares
# their whole declaration instead of just the statement — unless the base
# body is still a placeholder, the blueprint-fill case. Checked against a
# locally installed toolchain (leanprover--lean4---v4.32.0):
#
# - `def`, `abbrev` — the body is content. `def foo : Nat := 5` with
#   `theorem downstream : foo = 5 := by rfl` compiles; changing only the
#   body to `:= 6` leaves `downstream`'s text byte-identical and it stops
#   compiling. That is the hole this tuple exists to close.
# - `instance` — an instance body can be *data*, and typeclass resolution
#   supplies it implicitly, so a downstream theorem never names it: the
#   hardest redefinition for any statement-text audit to see. Pinning it
#   has a real cost, because a `Prop`-valued class instance genuinely IS
#   proof-irrelevant (`class Fact (p : Prop) : Prop`), so a legitimate
#   golf of one reads as a false `CHANGED`, and the keyword cannot tell
#   the two apart.
#
#   The tie breaks toward pinning on cost asymmetry, NOT because the
#   check is cheap to be wrong about — `statement-immutability` is
#   `CheckClass.TRUST` and blocks a merge on lean4 and isabelle
#   (`gate.checks.PROVER_OVERRIDES` relaxes rocq only), so a false
#   `CHANGED` here stops a merge rather than filing a report. What makes
#   that acceptable is the same property the whole promotion rested on:
#   a false block is one `choir-defect` comment from resolution and the
#   orchestrator decides (spec D5), whereas the miss is silent — nothing
#   downstream names the instance, so no other blocking check and no
#   human reading the diff is likely to catch a rewritten body.
#
#   Frequency here is UNMEASURED, unlike the rates that decided the
#   promotion: `scripts/measure_statement_immutability.py` samples
#   `proof_bearing=("theorem", "lemma", "example")` on lean4, so its
#   0.26% golf false-block rate does not cover instance golfing at all.
#   If that ever becomes noise in a real project, measure it before
#   removing `instance` from this tuple — dropping it reopens the
#   hardest-to-see redefinition in the language.
#
# Deliberately NOT here, i.e. proof-bearing and statement-compared:
#
# - `theorem`/`lemma` — a `theorem`'s type must be a `Prop`, verified by
#   rejection: `theorem notaprop : Nat := 5` fails with "type of theorem
#   `notaprop` is not a proposition". So the body is always a proof,
#   proof irrelevance applies, and `golf` keeps working. `lemma` is
#   Mathlib's macro expanding to `theorem` (core Lean 4 does not define
#   it), so it inherits the restriction.
# - `example` — anonymous, so nothing can reference it and no downstream
#   statement's meaning can follow its body.
_DEFINITION_KEYWORDS = ("def", "abbrev", "instance")

_KEYWORD = rf"(?:{'|'.join(_STATEMENT_KEYWORDS)})"

# Declaration modifiers (`ProverProfile.decl_modifiers`) — read off
# `Lean/Parser/Command.lean` in locally installed v4.32.0/v4.33.0
# toolchains:
#
#   declModifiers := docComment? attrs? visibility? protected?
#                     (meta | noncomputable)? unsafe? (partial | nonrec)?
#   visibility    := private | public
#
# `scoped`/`local` are not `declModifiers` themselves, but
# `«instance» := Term.attrKind >> "instance" >> ...` with
# `attrKind := optional (scoped | local)`, so they really do precede a
# decl keyword. This fragment is a shared prefix alternation across all
# keywords, not a per-keyword grammar — see
# `gate.provers.decl_syntax.decl_line_regex`'s docstring for why that
# over-approximation is deliberate.
#
# `opaque` is excluded: it is a decl-keyword alternative, not a modifier
# (see `_DECL_KEYWORDS`). `public` and `meta` were the largest fail-open
# the corpus measurement found — without them a `public theorem` line
# matched nothing at all, so its statement could be rewritten invisibly.
# Both live-compiled on v4.33.0 and present in v4.32.0's grammar.
#
# `expose` is deliberately NOT here: v4.33.0 spells it only as an
# *attribute* (`optional ("@[" >> nonReservedSymbol "expose" >> "]")`),
# never as a bare `declModifiers` word. `expose def foo : Nat := 1`
# fails to parse; `@[expose] public def ok : Nat := 1` compiles, and the
# `@[…]` form is already covered by `_ATTRIBUTE_SYNTAX` below.
_DECL_MODIFIERS = (
    "private",
    "public",
    "protected",
    "noncomputable",
    "meta",
    "partial",
    "unsafe",
    "nonrec",
    "scoped",
    "local",
)

# `@[attr1, attr2]` — `Term.attributes := "@[" >> ... >> "] "`.
_ATTRIBUTE_SYNTAX = ("@[", "]")

# Commands that take an argument and then scope a single declaration
# with `in` (`ProverProfile.decl_prefix_commands`). The `in` form is not
# part of either command's own production; it is a generic *trailing*
# parser that wraps one (`Lean/Parser/Command.lean`, v4.33.0):
#
#   «open»       := leading_parser  withPosition ("open" >> openDecl)
#   «set_option» := leading_parser  "set_option " >> ident >> ppSpace >>
#                                   optionValue
#   «in»         := trailing_parser withOpen (withSetOption
#                     (ppDedent (" in" >> ppLine >> commandParser)))
#
# `withOpen`/`withSetOption` are what make `open`/`set_option` the two
# commands it interprets; both forms live-checked on v4.32.0. Without
# them `open Nat in theorem foo : …` produced ZERO spans, so the
# declaration was invisible and its statement could be rewritten freely.
# Unlike a bare-word modifier the keyword alone is not the prefix — see
# `decl_prefix_fragment`'s docstring.
_DECL_PREFIX_COMMANDS = ("open", "set_option")

# Top-level commands that can never be part of a declaration's body, for
# `gate.verify.style._span_end`'s trailing-trim allowlist
# (`ProverProfile.non_body_commands` — read that field's note for the
# membership rule). Every atom below was read off a locally installed
# v4.33.0 toolchain's own parser, with the file it came from:
#
#   Lean/Parser/Command.lean:  "mutual"  "export "  "initialize " <|>
#     "builtin_initialize "  "include"  "omit"  "init_quot"
#     "add_decl_doc "  "recommended_spelling "
#   Lean/Parser/Syntax.lean:   "syntax "  "macro"  "macro_rules"  "elab"
#     "elab_rules"  "declare_syntax_cat"  "binder_predicate"
#     "infix"  "infixl"  "infixr"  "prefix"  "postfix"
#   Lean/Meta/Tactic/Grind/Parser.lean:  "grind_pattern "
#   Lean/Data/Options.lean:    "register_builtin_option"  "register_option"
#   Init/Notation.lean:        "run_cmd "  "run_meta "  "seal "  "unseal "
#   Init/NotationExtra.lean:   "unif_hint"
#   Init/Simproc.lean:         "builtin_simproc "  "builtin_dsimproc "
#     "builtin_simproc_decl "  "builtin_dsimproc_decl "
#   Init/Grind/Propagator.lean: "builtin_grind_propagator "
#
# `end`/`namespace`/`section`/`import`/`open`/`variable`/`set_option`/
# `universe`/`notation`/`attribute`/`deriving instance` are deliberately
# absent — already in `gate.verify.style._NON_BODY_TRAILER_RE`'s
# prover-blind core. The compiler's own file-local macros
# (`gen_injective_theorems%`, `declare_config_elab`, …) are absent too:
# they are defined inside `Lean/`'s own sources, so no project outside
# the compiler can write them. Residual this allowlist cannot close: any
# project may define its own `macro … : command`.
_NON_BODY_COMMANDS = (
    "mutual",
    "export",
    "initialize",
    "builtin_initialize",
    "include",
    "omit",
    "init_quot",
    "add_decl_doc",
    "recommended_spelling",
    "grind_pattern",
    "syntax",
    "macro",
    "macro_rules",
    "elab",
    "elab_rules",
    "declare_syntax_cat",
    "binder_predicate",
    "infix",
    "infixl",
    "infixr",
    "prefix",
    "postfix",
    "unif_hint",
    "run_cmd",
    "run_meta",
    "seal",
    "unseal",
    "register_builtin_option",
    "register_option",
    "builtin_simproc",
    "builtin_dsimproc",
    "builtin_simproc_decl",
    "builtin_dsimproc_decl",
    "builtin_grind_propagator",
)

# The shared modifier/attribute prefix fragment, embedded in front of
# `_KEYWORD` below so `extract_lean_statement`'s own boundary check is
# prefix-aware too: without it neither `@[simp] theorem foo` nor
# `private theorem foo` was extractable.
_DECL_PREFIX = decl_prefix_fragment(
    _DECL_MODIFIERS, _ATTRIBUTE_SYNTAX, prefix_commands=_DECL_PREFIX_COMMANDS
)

# Bracket pairs tracked by the balanced-paren scan. `⟨⟩` (Lean's
# anonymous constructor) is included because a structure literal inside
# it can contain `:=`.
_OPEN_TO_CLOSE = {"(": ")", "{": "}", "[": "]", "⟨": "⟩"}
_CLOSE_CHARS = set(_OPEN_TO_CLOSE.values())

# Identifier characters besides alphanumerics, so a literal `where`
# adjacent to one reads as part of a longer identifier (`whereClause`)
# rather than as the keyword.
_IDENT_CHAR_AFTER = "_'"


def _is_line_initial_pipe(text: str, i: int, decl_indent: int) -> bool:
    """True iff `text[i] == '|'` opens a `declValEqns` match-arm line.

    Per `Lean/Parser/Term.lean`'s `matchAlt`/`matchAlts` (`"| " >> …`,
    one per line), a `|` qualifies only when it is the first non-space
    character on its physical line — so a mid-line `|` inside a type or
    a nested `match`'s arms never does — and only when that line is
    indented to at least `decl_indent`, so a `|` that has fallen out of
    this declaration entirely does not either.
    """
    if text[i] != "|":
        return False
    line_start = text.rfind("\n", 0, i) + 1
    prefix = text[line_start:i]
    if prefix.strip():
        return False
    return len(prefix) >= decl_indent


def _is_darrow(text: str, i: int) -> bool:
    """True iff `text[i:]` starts with Lean's `darrow` token (`=>`).

    `Lean/Parser/Term.lean`: `darrow : Parser := " => "` and
    `matchAlt := "| " >> ppIndent (patterns >> darrow >> …)` — every
    `declValEqns` alternative contains a `=>`, and it is *required*, not
    optional. That is what lets `_scan_to_signature_end` tell a real
    match alternative from a line-initial `|` inside a type.
    """
    return text[i : i + 2] == "=>"


def _is_where_token(text: str, i: int) -> bool:
    """True iff `text[i:]` starts with a standalone `where` keyword.

    `Lean/Parser/Command.lean`'s `whereStructInst`
    (`"where" >> Term.structInstFields (sepByIndent ...)`, zero fields
    allowed) is real syntax for every `_STATEMENT_KEYWORDS` kind
    (`declVal := declValSimple <|> declValEqns <|> whereStructInst`);
    live-checked on v4.32.0 both with fields and with none. `where` is a
    reserved word, so the boundary check below is defensive.
    """
    n = len(text)
    if text[i:i + 5] != "where":
        return False
    if i > 0 and (text[i - 1].isalnum() or text[i - 1] in _IDENT_CHAR_AFTER):
        return False
    end = i + 5
    return not (end < n and (text[end].isalnum() or text[end] in _IDENT_CHAR_AFTER))


def _scan_to_signature_end(text: str, start: int, decl_indent: int) -> int | None:
    """Return the index ending the declaration's signature, or None.

    Lean's `declVal` (the right-hand side of a declaration) is exactly
    one of three shapes — `declValSimple <|> declValEqns <|>
    whereStructInst`, per `Lean/Parser/Command.lean` — and this scans for
    whichever of their leading tokens is reached first, left to right,
    all guarded by `depth == 0`:

    - a top-level `:=` (`declValSimple`) — returns the index just past
      it, so the signature keeps the `:=`.
    - a line-initial `|` at or deeper than `decl_indent` **that a `=>`
      confirms** (`declValEqns`, an equation-style definition) — returns
      the index of the `|` itself, excluding it; the caller's `.strip()`
      drops the whitespace this leaves.
    - a standalone `where` (`whereStructInst`) — returns the index just
      past the keyword.

    **Why the `|` needs confirming.** Terminating at *every* line-initial
    `|` was a fail-open: a *statement* wrapping onto a line beginning
    with `|` — Mathlib writes `|a - b|` for `abs` and wraps at the
    operator — was truncated at the bar, so a real rewrite of such a
    conclusion compared equal and read UNCHANGED. Narrowing by
    declaration *kind* does not work, and the toolchain says so:
    `«theorem» := "theorem " >> declId >> declSig >> declVal` over the
    same three-way `declVal`, so `theorem` admits equation alternatives
    exactly as `def` does (`theorem tfun : Nat → True` with `| 0 =>
    trivial` arms compiles on v4.32.0). What does distinguish them is
    `matchAlt := "| " >> ppIndent (patterns >> darrow >> …)`: every
    alternative contains a `=>`. So a candidate `|` is remembered rather
    than acted on, and terminates the signature only if a top-level `=>`
    is reached before a top-level `:=` or `where`.

    Residual: an *unbracketed* `fun x => …` at depth zero inside a type,
    after a line-initial `|`, still misfires. (Mathlib's `fun x ↦ …`
    cannot — `darrow` is the ASCII `=>` token specifically.)

    Returns None if none of the three is found in the remaining text
    (malformed or truncated input, or a candidate `|` that nothing
    confirmed).
    """
    depth = 0
    i = start
    n = len(text)
    pending_pipe: int | None = None
    while i < n:
        c = text[i]
        if c in _OPEN_TO_CLOSE:
            depth += 1
            i += 1
            continue
        if c in _CLOSE_CHARS:
            # Tolerate stray closers; malformed input bottoms out at zero.
            depth = max(0, depth - 1)
            i += 1
            continue
        if depth == 0:
            if (
                c == "|"
                and pending_pipe is None
                and _is_line_initial_pipe(text, i, decl_indent)
            ):
                # Only a top-level `=>` below confirms this as a
                # `declValEqns` arm rather than part of the type.
                pending_pipe = i
                i += 1
                continue
            if c == "=" and pending_pipe is not None and _is_darrow(text, i):
                return pending_pipe
            if c == "w" and _is_where_token(text, i):
                return i + 5
            if c == ":" and i + 1 < n and text[i + 1] == "=":
                return i + 2
        i += 1
    return None


def _decl_indentation(text: str, match: re.Match[str]) -> int:
    """Return the column at which the matched declaration itself begins.

    The prefix regex anchors with `(?m)^\\s*`, and that `\\s*` is greedy
    and `\\s` matches newlines, so `match.start()` can sit one or more
    blank lines above the declaration's own line.
    """
    leading_ws = len(match.group()) - len(match.group().lstrip())
    decl_start = match.start() + leading_ws
    line_start = text.rfind("\n", 0, decl_start) + 1
    return decl_start - line_start


def extract_lean_statement(text: str, decl_name: str) -> str | None:
    """Return the signature text for `decl_name` — the keyword through
    whichever of `declVal`'s three shapes ends it (see
    `_scan_to_signature_end`'s docstring).

    Looks for the declaration by either its full path (`Foo.bar`) or its
    last segment (`bar`); the file may have either form depending on
    whether it is inside a `namespace` block. The `:=` route is
    depth-aware, so a `:=` inside `(n : Nat := 0)` or `{α : Type := ...}`
    does not truncate the signature.

    Returns `None` if no match is found, or if the declaration's
    `declVal` could not be located by any of the three routes. Naive
    about Lean comments and Unicode identifiers; planned iteration.
    Leading modifiers and attribute lists are skipped via `_DECL_PREFIX`
    before the keyword is matched, so the returned text includes
    whatever prefix preceded the keyword on the line.
    """
    last_segment = decl_name.rsplit(".", 1)[-1]
    if last_segment == decl_name:
        name_alternation = re.escape(decl_name)
    else:
        # Try fully-qualified first so we prefer that over the last-segment
        # form when both could match (only matters for namespace shadowing).
        name_alternation = f"{re.escape(decl_name)}|{re.escape(last_segment)}"

    # Just the prefix (keyword + name + identifier boundary); the
    # signature body is consumed by `_scan_to_signature_end`, which
    # tracks paren depth so a nested `:=` cannot terminate the match.
    prefix_re = re.compile(
        rf"(?m)^\s*{_DECL_PREFIX}{_KEYWORD}\s+(?:{name_alternation})(?![A-Za-z0-9_.'])",
    )
    match = prefix_re.search(text)
    if match is None:
        return None
    body_start = match.end()
    decl_indent = _decl_indentation(text, match)
    end = _scan_to_signature_end(text, body_start, decl_indent)
    if end is None:
        return None
    return text[match.start():end].strip()


# ---------------------------------------------------------------------------
# Namespace qualification (design note 12 §4.1).
# ---------------------------------------------------------------------------

_NAMESPACE_OPEN_RE = re.compile(r"^\s*namespace\s+(\S+)")
_SECTION_OPEN_RE = re.compile(r"^\s*section\b(?:\s+(\S+))?")
_END_RE = re.compile(r"^\s*end\b(?:\s+(\S+))?\s*$")


def _pop_innermost(stack: list[tuple[str, str | None]], name: str | None) -> None:
    """Pop `stack` for an `end` (`name=None`) or `end <name>` command.

    Bare `end` pops whichever entry is innermost, section or namespace
    alike. `end <name>` removes the topmost entry with that name
    (tolerating out-of-order closes in malformed input), falling back to
    popping the innermost so the stack still converges.
    """
    if not stack:
        return
    if name is None:
        stack.pop()
        return
    for i in range(len(stack) - 1, -1, -1):
        if stack[i][1] == name:
            del stack[i]
            return
    stack.pop()


def qualify_decl_names(text: str) -> dict[int, str]:
    """Map each declaration's line number to its fully-qualified name.

    Tracks `namespace X` / `section` (optionally `section X`) opens on
    one stack, so `end`/`end <name>` accounting stays correct even when
    sections and namespaces interleave — only `namespace` entries
    contribute to the qualified name. Outside any namespace, the surface
    name is returned unchanged.

    Deliberately NOT replaced by
    `gate.provers.decl_syntax.qualify_by_scope`: that helper's
    `plain_open_re` discards a section's *name*, so `end A` closing a
    section named `A` inside `namespace A` pops the namespace instead of
    the section. That shape compiles on v4.32.0/v4.33.0 and resolves as
    `A.t`, which only this implementation reports.

    v0, text-level only, imperfect by construction: `open ... in`-style
    scoping tricks are out of scope (design note 12 §4.1).
    """
    stack: list[tuple[str, str | None]] = []
    result: dict[int, str] = {}
    lines = text.splitlines()
    for line_no, line in enumerate(lines, start=1):
        ns_match = _NAMESPACE_OPEN_RE.match(line)
        if ns_match:
            stack.append(("namespace", ns_match.group(1)))
            continue
        section_match = _SECTION_OPEN_RE.match(line)
        if section_match:
            stack.append(("section", section_match.group(1)))
            continue
        end_match = _END_RE.match(line)
        if end_match:
            _pop_innermost(stack, end_match.group(1))
            continue
        decl_match = _DECL_LINE_RE.match(line)
        if decl_match:
            namespaces = [name for kind, name in stack if kind == "namespace"]
            surface = decl_name_from(decl_match, lines, line_no - 1)
            result[line_no] = ".".join([*namespaces, surface]) if namespaces else surface
    return result


# ---------------------------------------------------------------------------
# Trust-report hooks (design note 12 §4).
#
# Probe: a scratch file importing whatever modules the caller names,
# then one `#print axioms <decl>` per target. Run via `lake env lean` so
# the project's own search path (built `.lake` artifacts, dependencies)
# is in scope. Output shapes confirmed by a live run (see the
# integration test in tests/gate/provers/test_trust.py):
#
#     't' does not depend on any axioms
#     'uses_bad' depends on axioms: [bad]
#     'uses_two' depends on axioms: [bad, bad2]
#
# A probe referencing an unknown declaration prints a compile error on
# *stdout* (not stderr) and exits nonzero; the parser ignores unmatched
# lines and `gate.provers.trust.collect_trust_report` turns the nonzero
# exit into a `ProverError` (stdout as the detail, since stderr is empty).
# ---------------------------------------------------------------------------

_TRUST_PROBE_FILENAME = ".choir-trust-probe.lean"

_DEPENDS_ON_AXIOMS_RE = re.compile(
    r"^'(?P<decl>.+)' depends on axioms: \[(?P<axioms>.*)\]$"
)
_NO_AXIOMS_RE = re.compile(r"^'(?P<decl>.+)' does not depend on any axioms$")


def build_lean_trust_probe(
    workspace: Path, targets: list[str], imports: list[str]
) -> Path:
    """Write `<workspace>/.choir-trust-probe.lean` and return its path.

    One `import <module>` line per `imports` entry (in order), then
    one `#print axioms <decl>` line per target (in order).
    """
    lines = [f"import {module}" for module in imports]
    lines.extend(f"#print axioms {decl}" for decl in targets)
    probe_path = workspace / _TRUST_PROBE_FILENAME
    probe_path.write_text("\n".join(lines) + "\n")
    return probe_path


def lean_trust_report_command(
    workspace: Path, targets: list[str], imports: list[str]
) -> list[str]:
    """Write the trust probe and return the `lake env lean` invocation."""
    build_lean_trust_probe(workspace, targets, imports)
    return ["lake", "env", "lean", _TRUST_PROBE_FILENAME]


def parse_lean_trust_report(output: str) -> list[TrustEntry]:
    """Parse `lake env lean` output into `TrustEntry` records.

    Lines matching neither shape (compile errors, build noise, blank
    lines) are ignored.
    """
    entries: list[TrustEntry] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        match = _DEPENDS_ON_AXIOMS_RE.match(line)
        if match:
            axioms_text = match.group("axioms").strip()
            assumptions = (
                tuple(a.strip() for a in axioms_text.split(","))
                if axioms_text
                else ()
            )
            entries.append(
                TrustEntry(decl=match.group("decl"), assumptions=assumptions, clean=False)
            )
            continue
        match = _NO_AXIOMS_RE.match(line)
        if match:
            entries.append(TrustEntry(decl=match.group("decl"), assumptions=(), clean=True))
    return entries


# ---------------------------------------------------------------------------
# Dependency probe.
#
# Probe: a scratch file, written outside the project, importing the
# project's modules, then one pass
# over the environment reporting every declaration those modules
# elaborate. Run via `lake env lean`, like the trust probe, so the
# project's built `.lake` artifacts are in scope; it reads the compiled
# environment and never re-elaborates, so the cost is the import.
#
# One tab-separated `choir-dep` line per declaration:
#
#     choir-dep<TAB>kind<TAB>module<TAB>line<TAB>state<TAB>decl<TAB>uses<TAB>proof_uses
#
# `uses` and `proof_uses` are comma-separated and may be empty. The
# marker prefix is what lets the parser ignore build noise, warnings and
# blank lines the way `parse_lean_trust_report` does.
#
# Three details in the probe body are load-bearing:
#
#   * `ConstantInfo.value?` is `None` for an imported theorem, so a
#     proof's dependencies must be read through `getUsedConstantsAsSet`
#     — the same traversal `#print axioms` uses. Reading `value?` would
#     silently report every imported theorem as depending on nothing.
#   * A `private` declaration's name is mangled, so `privateToUserName?`
#     has to run before the module filter and before any name is
#     reported, or every private helper vanishes from the result.
#   * The environment holds generated declarations a project never wrote
#     (congruence lemmas, equation lemmas). `findDeclarationRanges?`
#     separates them: a declaration the source declares has a range, and
#     the generated ones do not.
#
# It enumerates the project's own modules out of the environment header
# rather than filtering the whole constant map. Both answer the same
# question, and on a project sitting on a large library they are not
# remotely the same price: the map holds every declaration of every
# dependency, and asking each one which module it came from costs more
# than a hundred times the work of reading the handful of modules the
# project wrote.
# ---------------------------------------------------------------------------

_DEPENDENCY_PROBE_FILENAME = ".choir-deps-probe.lean"

_DEPENDENCY_MARKER = "choir-dep"

_DEPENDENCY_PROBE_BODY = r"""open Lean in
#eval show CoreM Unit from do
  let env ← getEnv
  let project : NameSet := PROJECT_MODULES.foldl (fun s m => s.insert m) {}
  let watched : NameSet :=
    (WATCHED_DECLS : List String).foldl (fun s n => s.insert n.toName) {}
  let user : Name → Name := fun n => (privateToUserName? n).getD n
  let modules := env.header.moduleNames
  let mine : Array Nat := (List.range modules.size).toArray.filter
    (fun i => project.contains modules[i]!)
  let declared : NameSet := mine.foldl (fun s i =>
    env.header.moduleData[i]!.constNames.foldl
      (fun s n => if (user n).isInternalDetail then s else s.insert (user n)) s)
    watched
  let join : List Name → String := fun ms => String.intercalate "," (ms.map toString)
  let mut rows : Array String := #[]
  for i in mine do
    for n in env.header.moduleData[i]!.constNames do
      let self := user n
      if self.isInternalDetail then continue
      let some ci := env.find? n | continue
      let some ranges ← findDeclarationRanges? n | continue
      let kind := match ci with
        | .thmInfo _ => "theorem"
        | .axiomInfo _ => "axiom"
        | _ => "definition"
      let inType : NameSet :=
        ci.type.getUsedConstants.foldl (fun s m => s.insert (user m)) {}
      let every := ci.getUsedConstantsAsSet.toList
      let reached : NameSet := every.foldl
        (fun s m => if declared.contains (user m) then s.insert (user m) else s) {}
      let named := reached.toList.filter (fun m => m != self)
      rows := rows.push <| String.intercalate "\t"
        ["MARKER", kind, toString modules[i]!, toString ranges.range.pos.line,
         if every.any (fun m => m == `sorryAx) then "placeholder" else "complete",
         toString self,
         join (named.filter (fun m => inType.contains m)),
         join (named.filter (fun m => !inType.contains m))]
  for r in rows.qsort (· < ·) do IO.println r
"""


def build_lean_dependency_probe(
    workspace: Path, imports: list[str], watched: list[str] | None = None
) -> Path:
    """Write `<workspace>/.choir-deps-probe.lean` and return its path.

    `imports` are the project's own modules: they are both what the probe
    imports and the set it reports on, so a module left out is invisible
    rather than half-reported.

    `watched` are library declarations to report as dependencies even
    though the project does not declare them. Reporting every library
    constant a proof term reaches would bury the plan under mathlib, so
    the probe reports none of them by default; a watched name is one the
    roadmap already records as an `upstream` node, which makes it a
    declaration the plan is about rather than an implementation detail
    of one. A name that no longer exists, or never did, simply never
    matches — the probe reads the environment and does not resolve this
    list against it.

    Names are passed as strings and parsed with `String.toName` rather
    than spliced in as name literals, so a declaration whose identifier
    needs guillemet quoting cannot produce a probe that fails to parse.
    """
    modules = "[" + ", ".join(f"`{module}" for module in imports) + "]"
    # `ensure_ascii=False` keeps a subscript or a Greek letter as itself:
    # the probe is written as UTF-8 and Lean reads it as UTF-8, whereas a
    # `\uXXXX` escape would depend on how Lean spells one.
    names = "[" + ", ".join(
        json.dumps(decl, ensure_ascii=False) for decl in (watched or [])
    ) + "]"
    body = (
        _DEPENDENCY_PROBE_BODY.replace("PROJECT_MODULES", modules)
        .replace("WATCHED_DECLS", names)
        .replace("MARKER", _DEPENDENCY_MARKER)
    )
    # `import Lean` is the probe's own dependency, not the project's: the
    # metaprogramming API it reads the environment through is not in scope
    # for a project that doesn't already pull it in.
    lines = ["import Lean", *(f"import {module}" for module in imports)]
    probe_path = workspace / _DEPENDENCY_PROBE_FILENAME
    probe_path.write_text("\n".join(lines) + "\n" + body)
    return probe_path


def lean_dependency_command(
    workspace: Path, imports: list[str], watched: list[str] | None = None
) -> list[str]:
    """Write the dependency probe and return the `lake env lean` invocation.

    `workspace` is where the probe is written, which is not the directory
    it is run in, so the path is absolute.
    """
    probe_path = build_lean_dependency_probe(workspace, imports, watched)
    return ["lake", "env", "lean", str(probe_path)]


def _names(field: str) -> tuple[str, ...]:
    return tuple(part for part in field.split(",") if part)


def parse_lean_dependencies(output: str) -> list[DeclDependency]:
    """Parse `lake env lean` output into `DeclDependency` records.

    Lines without the marker, or with the wrong field count, are ignored:
    a probe run shares stdout with the build's own diagnostics.
    """
    found: list[DeclDependency] = []
    for raw_line in output.splitlines():
        fields = raw_line.rstrip("\n").split("\t")
        if len(fields) != 8 or fields[0].strip() != _DEPENDENCY_MARKER:
            continue
        _, kind, module, line, state, decl, uses, proof_uses = fields
        if not line.isdigit():
            continue
        found.append(
            DeclDependency(
                decl=decl,
                kind=kind,
                module=module,
                line=int(line),
                has_placeholder=state == "placeholder",
                uses=_names(uses),
                proof_uses=_names(proof_uses),
            )
        )
    return found


# ---------------------------------------------------------------------------
# The profile
# ---------------------------------------------------------------------------

LEAN4 = ProverProfile(
    name="lean4",
    file_extensions=(".lean",),
    comment_syntax=CommentSyntax(line="--", block_open="/-", block_close="-/"),
    decl_keywords=_DECL_KEYWORDS,
    statement_keywords=_STATEMENT_KEYWORDS,
    definition_keywords=_DEFINITION_KEYWORDS,
    placeholder_tokens=("sorry",),
    trust_patterns=(
        ("axiom", r"\baxiom\s+\w"),
        ("unsafe", r"\bunsafe\b"),
        ("partial", r"\bpartial\b"),
        ("native_decide", r"\bnative_decide\b"),
        ("extern", r"\bextern\b"),
    ),
    build_command=("lake", "build"),
    toolchain_file="lean-toolchain",
    protected_files=(
        "lakefile.toml",
        "lakefile.lean",
        "lean-toolchain",
        "lake-manifest.json",
    ),
    extra_audits=(),
    search_tooling_note=True,
    extract_statement=extract_lean_statement,
    trust_report_command=lean_trust_report_command,
    parse_trust_report=parse_lean_trust_report,
    decl_modifiers=_DECL_MODIFIERS,
    attribute_syntax=_ATTRIBUTE_SYNTAX,
    decl_prefix_commands=_DECL_PREFIX_COMMANDS,
    non_body_commands=_NON_BODY_COMMANDS,
    qualify_decl_names=qualify_decl_names,
    freshness_command=("lake", "build", "--no-build"),
    dependency_command=lean_dependency_command,
    parse_dependencies=parse_lean_dependencies,
)

# `qualify_decl_names`'s declaration-boundary regex. Read off the profile
# so every prefix shape stays in step with the other `gate/` scanners —
# a hand-written argument list here is the drift `decl_line_regex_for`
# exists to prevent. Defined after `LEAN4` because it reads it; the only
# use is inside `qualify_decl_names`, so the forward reference resolves.
_DECL_LINE_RE = decl_line_regex_for(LEAN4)
