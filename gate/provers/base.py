"""Frozen dataclasses for prover profiles (design note 12 §2.2).

A `ProverProfile` is Choir-shipped code, never project config — see
`gate/provers/__init__.py` and design note 12 §2.1 for why letting a
repo define its own trust tokens would let a PR weaken its own gate.
This module holds only the shapes; profile *instances* live in
`lean4.py` / `isabelle.py` / `rocq.py`.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CommentSyntax:
    r"""A prover's comment delimiters, for comment-aware scanning.

    `line` is `None` for a prover with no line-comment syntax (isabelle
    and rocq have only `(* … *)`). `verbatim_delimiters` is an
    `(open, close)` pair whose *content the prover's own lexer does not
    scan for comment markers* — isabelle's cartouche, `\<open>…\<close>`,
    which nests and inside which `(*` is ordinary text; `None` (lean4,
    rocq) means the prover has no such bracket.

    The cartouche is a trust surface, not a nicety. `lift_definition
    times_word … is \<open>(*)\<close>` — HOL's multiplication operator,
    `HOL/Library/Word.thy:55` — is one ordinary line that without it
    reads as a comment that never closes, so every comment-blanking
    consumer blanks the rest of the file, and an invisible declaration's
    statement can be rewritten freely: `statement-equiv`
    (`CheckClass.TRUST` on isabelle) reports `UNDETERMINED`, which
    passes.

    **ASCII only, deliberately.** The Unicode rendering `‹…›`
    (U+2039/U+203A) is NOT accepted by the prover back-end: `isabelle
    build` rejects a raw `‹` with *Malformed command syntax*, and
    `Doc/JEdit/JEdit.thy` §sec:symbols states formal text is ASCII and
    raw Unicode belongs only in informal parts. Handling it would be
    actively unsafe — a raw `›` *inside* an ASCII cartouche is legal
    informal text that BUILDS (verified), so treating it as a closer
    would end cartouche state early and re-open the fail-open above.
    """

    line: str | None
    block_open: str
    block_close: str
    nested: bool = True
    verbatim_delimiters: tuple[str, str] | None = None


@dataclass(frozen=True)
class TrustEntry:
    """One declaration's trust boundary, from an environment-level probe.

    `clean=True` (with `assumptions=()`) means the declaration is
    closed under the kernel / global context — see design note 12 §4.
    Otherwise `assumptions` names the axioms/oracles it depends on.
    """

    decl: str
    assumptions: tuple[str, ...]
    clean: bool


@dataclass(frozen=True)
class DeclDependency:
    """One declaration and the project-internal declarations it draws on.

    `uses` are the declarations its *statement* mentions; `proof_uses`
    the ones only its body reaches. That is the split a project's
    `roadmap/graph.json` records under the same two names, so a plan can
    be regenerated from the environment instead of maintained by hand.

    Both lists name only declarations from the project's own modules. A
    proof term reaches the whole library, and an edge to `Nat.add_comm`
    says nothing about the plan — the project's own lemmas are what the
    roadmap is a map of.

    `module` is the module the declaration is elaborated in, not a path:
    only the caller knows where a module's source sits, and guessing a
    layout from the name would be wrong for any project whose library
    root is not its source root. `has_placeholder` is read from the
    elaborated term rather than the source text, so it is true exactly
    when the prover would accept the declaration only under its
    placeholder axiom.
    """

    decl: str
    kind: str
    module: str
    line: int
    has_placeholder: bool
    uses: tuple[str, ...]
    proof_uses: tuple[str, ...]


@dataclass(frozen=True)
class ProverProfile:
    r"""Everything Choir's gate needs to know about one prover.

    Declarative fields come from design note 12 §2.2/§3; the callables
    are prover-specific implementations wired in each prover module.
    Field contract follows.

    Identity and build
    ------------------
    `name` is the registry id and the `.choir/project.toml`
    `[project] prover` value. `file_extensions`, `build_command`,
    `toolchain_file` (`None` for a prover with no pinned-toolchain
    file), `protected_files` (paths a worker may never edit).
    `trust_patterns` is `(label, regex-source)` pairs — the prover's
    trust-token vocabulary, compiled by `gate.verify.axiom_honesty`.
    `extra_audits` names prover-only audits (none at present).
    `search_tooling_note` gates the client's
    Mathlib-search advisory (lean4 only).

    Trust-report probe (design note 12 §4)
    --------------------------------------
    `trust_report_command(workspace, targets, imports)` writes whatever
    probe file the prover needs (side effect) and returns the argv to
    run it; `imports` are the modules/theories/sessions the probe needs
    in scope (the CLI's repeatable `--import`).
    `parse_trust_report(output)` turns the captured stdout into
    `TrustEntry` records. `one_probe_per_decl` is True when the probe
    must run once per declaration because its output does not echo the
    queried name (rocq's `Print Assumptions`, where
    `gate.provers.trust.collect_trust_report` attributes the result
    itself); False for lean4 and isabelle, whose output names each
    declaration.

    Build freshness
    ---------------
    `freshness_command` asks the build tool whether the compiled
    artifacts are up to date with the source, without building
    anything — lean4's `lake build --no-build`, which exits nonzero
    when a target is out of date. `None` for a prover with no such
    query. It answers the one question a reader of compiled artifacts
    cannot answer for itself, since importing a module whose source has
    changed yields its last built state and says nothing.

    Dependency probe
    ----------------
    `dependency_command(workspace, imports, watched)` writes a probe that
    reports every declaration the named modules elaborate, together with
    the project-internal declarations each one draws on, and returns the
    argv to run it; `parse_dependencies(output)` turns the captured
    stdout into `DeclDependency` records. `watched` names library
    declarations to report as well — the probe drops library constants
    otherwise, so this is how an edge to a lemma the roadmap names but
    the project does not declare survives. Both are `None` for a prover with no
    way to enumerate a declaration's dependencies from a built
    environment, and `gate.provers.deps.collect_dependencies` reports
    that as an unsupported prover rather than an empty result — an empty
    dependency list is indistinguishable from a project whose
    declarations genuinely reference nothing, and a caller that wrote it
    into a plan would erase every edge the plan had.

    Declaration kinds
    -----------------
    `decl_keywords` is every declaration keyword. `statement_keywords`
    is the subset whose kinds `extract_statement(text, name)` resolves a
    statement/body split for; `definition_keywords` is a further subset
    of *that*. The three resulting groups partition `decl_keywords`, and
    `tests/gate/verify/test_statement_immutability.py` pins each
    profile's membership so a newly-added keyword cannot land in
    whichever group happens to be the default:

    - **body-less** — outside `statement_keywords` (lean4
      `structure`/`class`/`inductive`/`axiom`/`opaque`; isabelle and
      rocq have none). Nothing in it is the worker's to change, so
      callers compare the whole declaration span instead of calling
      `extract_statement`.
    - **proof-bearing** — in `statement_keywords`, not in
      `definition_keywords`. The body is a kernel-checked proof and any
      valid proof of the statement will do, so a body rewrite is
      permitted (a `golf` task is exactly that) and only the statement
      is compared. lean4's `theorem` is the clean case: the toolchain
      refuses `theorem notaprop : Nat := 5` outright ("type of theorem
      `notaprop` is not a proposition"), so the body is always a proof
      of a `Prop`.
    - **definition-bearing** — in `definition_keywords` (defaults to
      `()`). The body *is* the content, so `def foo : Nat := 5` becoming
      `:= 6` changes what every downstream mention asserts with no
      statement text changing anywhere. Compared **whole**, except when
      the base body is still a `placeholder_tokens` token (an unfilled
      blueprint node), where the statement alone is compared.

    There is deliberately no `definition_placeholder_tokens` field —
    isabelle's `undefined` is also ordinary HOL content, so keying the
    escape on it made real definition bodies freely rewritable. See
    `gate.provers.isabelle`'s `_DEFINITION_BODY_SEPARATOR` comment for
    why the field was removed rather than narrowed. An isabelle
    definition fill therefore reports `CHANGED` — a false block, the
    right direction, and `docs/agents/ORCHESTRATOR.md` § Stage two already has
    the orchestrator review an `undefined` body by hand.

    `definition_body_separator` is the prover's whole-word token that
    starts a definition's body (isabelle `where`), or `None`. Set for
    isabelle alone, because its `extract_statement` captures the header
    *including* the `where` clause, so without truncating both sides
    there the placeholder escape above changes nothing; lean4's and
    rocq's extractors already stop before the body, where masking would
    only risk dropping signature text.

    `placeholder_tokens` is the prover's *proof*-placeholder vocabulary
    (lean4 `sorry`; isabelle `sorry`/`oops`; rocq `Admitted`/`admit`/
    `Abort`). It feeds the blocking `sorry-delta` audit and the
    worker-facing "placeholders you may not add" list, so an
    ordinary-content token must not be added to it.

    Declaration boundaries
    ----------------------
    Seven fields describing where a declaration starts, all consumed by
    `gate.provers.decl_syntax.decl_line_regex`. Prefer
    `decl_line_regex_for(profile)` over threading them by hand: a
    consumer that misses one silently keeps the fail-open the field was
    added to close. Every value is transcribed from the prover's real
    grammar (provenance notes live in each prover module) — inventing
    one is worse than omitting it, since a non-declaration line read as
    a boundary shifts a neighbour's span and false-blocks *it*.

    Before the keyword:

    - `decl_modifiers` — real declaration-modifier keywords, matched
      literally (lean4 `private`/`protected`/…, isabelle
      `private`/`qualified`).
    - `attribute_syntax` — `(open, close)` bracket pair for an
      attribute list that may precede a declaration on the same line
      (lean4 `@[`/`]`, rocq `#[`/`]`). `None` for isabelle, which
      attaches attributes *after* the name (`lemma foo[simp]:`) —
      handled by `decl_syntax.normalize_decl_name`.
    - `decl_prefix_commands` — a command taking an argument and then
      scoping one declaration with `in` (lean4's `open Nat in theorem
      foo`, `set_option … in theorem foo`). lean4 only: isabelle's
      `context … begin` and rocq's `Section … End` are block wrappers,
      already handled as ordinary intervening lines.
    - `decl_prefix_flags` — a prefix keyword taking a *terminated*
      argument and then the declaration, with no `in` and no closing
      bracket (rocq's `control_flag`s, `Time Definition foo := 5.`).
      rocq only. Raw regex fragments rather than literals; see
      `decl_syntax.decl_prefix_fragment` for the two rules such a
      fragment must satisfy.

    Between the keyword and the name — isabelle only so far, all raw
    regex fragments under the same no-capturing-groups rule:

    - `decl_target_syntax` — an optional target specification,
      isabelle's `lemma (in A) foo:` (the standard way to target a
      locale, so a large fraction of real theorems). Matched
      *optionally*, which is load-bearing in two directions: a
      declaration written without a target still matches, and one whose
      name sits on the *next* line backtracks to the old raw-token name
      `(in` instead of vanishing from enumeration.
    - `decl_keyword_suffix` — decorations attached to the keyword with
      no whitespace between them (`definition\<^marker>\<open>…\<close>
      foo`, `lift_definition(code_dt) foo`, `theorem%important foo`),
      which made the pattern fail to match at all.
    - `decl_type_params_syntax` — type parameters written *before* the
      name (`datatype ('a, 'b) t`, `typedef (overloaded) ('a, 'b) vec`).
      Not a fail-open — the declaration is enumerated — but keyed on
      `('a,` rather than a name, so every same-keyword type declaration
      in a theory collided. Keep it separate from `decl_target_syntax`:
      merging the two into one order-free alternation would match inputs
      the ordered pair rejects.

    `decl_continuation_lines(lines) -> {0-based index}` is not a
    boundary shape but the same enumeration's missing memory: lines that
    BEGIN inside a region where the prover's own lexer is mid-token, so
    a line that looks like a declaration start is not one. isabelle has
    such regions — `definition ff :: "(nat, nat)` / `fun " where …`
    builds, and `fun` there is HOL's postfix function-type constructor
    inside an open quoted type, which split one declaration into a
    truncated span plus a phantom named `"`. A prover-supplied callable
    rather than a delimiter this module interprets, because the rule
    needs the prover's lexer: isabelle's must track `"…"` and
    `\<open>…\<close>` jointly, since either can contain the other's
    delimiter as ordinary text (a naive quote-parity version lost 931
    real declarations to one unbalanced `"` in a prose cartouche).
    `None` (lean4, rocq) means no such rule. Consumers reach it through
    `decl_syntax.continuation_lines_for`, fed comment-blanked lines.

    **The failure direction here is the opposite of every prefix
    shape's**: those risk inventing a boundary (a false block), whereas
    a mis-read continuation region SUPPRESSES declarations, which is a
    fail-open. Treat a change to a profile's implementation as
    trust-affecting and measure it against a real corpus.

    `reserved_non_names` — tokens that can never be a declaration's NAME
    however name-shaped they look. When a declaration keyword's own line
    ends, the name comes from the next line, and if that line opens a
    clause instead (isabelle `lemma` / newline / `assumes …`) the
    captured token is a reserved word and the declaration is anonymous.
    Shape cannot tell them apart: `assumes` is a perfectly good
    identifier. Empty for a prover with no such form.

    `non_body_commands` is the prover's own top-level command vocabulary
    for `gate.verify.style._span_end`'s trailing-trim allowlist. A
    declaration's span runs to the last line that allowlist does not
    recognize as *not* body, so a real command it does not name is
    attributed to the preceding declaration — and where that declaration
    is compared whole-span the resulting false block is certain, not
    probable. Membership rule: *this command cannot be part of a proof*.
    On isabelle that is mechanical — Isabelle's own `keywords`
    declarations classify every command, and any theory-level kind
    (`thy_decl`, `thy_defn`, `thy_goal`, …) is by construction not
    writable inside a proof; a `diag`/`document_body` command *is*
    writable there, so those are admitted one at a time and only when
    goal-free (`value`, `term`, `thm`), never goal-*consuming*
    (`nitpick`, `sledgehammer`, `try`), because a line invoking one sits
    between an unproved goal and its `oops`. lean4 has no kind table, so
    every entry was read off the toolchain's parser
    (`Lean/Parser/Command.lean`, `Syntax.lean`, `Init/*.lean`). Defaults
    to `()`; rocq sets nothing — no rocq corpus or toolchain was
    available to derive a list from, and it is the prover with the
    largest whole-span population. This lowers the class's frequency
    without closing it: an allowlist of "lines that are not body" is
    unbounded, and all three provers let a project define new top-level
    commands (isabelle `ML`/`setup`, lean4 `macro`/`elab`/`syntax`).

    `qualify_decl_names(text) -> {1-indexed line: qualified name}` maps a
    declaration to a key distinguishing two same-*surface*-named
    declarations in different scopes, for callers that group by name
    (`gate.verify.statement_immutability`'s uniqueness check). `None`
    means no such rule and the caller falls back to the surface name.
    All three profiles set it, but only lean4's output is a name Lean
    would resolve: isabelle's and rocq's are enclosing-scope paths built
    on `gate.provers.decl_syntax.qualify_by_scope` and deliberately NOT
    name resolution (`Include`, functor application, locale
    interpretation and `sublocale` need semantics no toolchain here can
    verify). Sound for grouping, since a key is only ever matched
    against another key computed the same way over the same file; NOT
    sound for a caller that hands the name to the prover — see
    `gate.verify.changed_decls._extract`, which uses lean4's function
    directly for trust-report probe targets. Read `qualify_by_scope`'s
    docstring before adding a consumer.
    """

    name: str
    file_extensions: tuple[str, ...]
    comment_syntax: CommentSyntax
    decl_keywords: tuple[str, ...]
    statement_keywords: tuple[str, ...]
    placeholder_tokens: tuple[str, ...]
    trust_patterns: tuple[tuple[str, str], ...]
    build_command: tuple[str, ...]
    toolchain_file: str | None
    protected_files: tuple[str, ...]
    extra_audits: tuple[str, ...]
    search_tooling_note: bool
    extract_statement: Callable[[str, str], str | None]
    trust_report_command: Callable[[Path, list[str], list[str]], list[str]]
    parse_trust_report: Callable[[str], list[TrustEntry]]
    one_probe_per_decl: bool = False
    reserved_non_names: tuple[str, ...] = ()
    definition_keywords: tuple[str, ...] = ()
    definition_body_separator: str | None = None
    decl_modifiers: tuple[str, ...] = ()
    attribute_syntax: tuple[str, str] | None = None
    decl_prefix_commands: tuple[str, ...] = ()
    decl_prefix_flags: tuple[str, ...] = ()
    decl_target_syntax: str | None = None
    decl_keyword_suffix: str | None = None
    decl_type_params_syntax: str | None = None
    decl_continuation_lines: Callable[[Sequence[str]], frozenset[int]] | None = None
    non_body_commands: tuple[str, ...] = ()
    qualify_decl_names: Callable[[str], dict[int, str]] | None = None
    freshness_command: tuple[str, ...] | None = None
    dependency_command: Callable[[Path, list[str], list[str]], list[str]] | None = None
    parse_dependencies: Callable[[str], list[DeclDependency]] | None = None
