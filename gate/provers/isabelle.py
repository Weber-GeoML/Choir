"""The isabelle prover profile (design note 12 §2.2, §3.2).

Declarative fields come from design note 12's field table;
`decl_keywords` does not — it is derived from the live toolchain's own
Isar keyword-kind table, so the note's row is a snapshot and the tuple
below is the source of truth.

`extract_isabelle_statement` implements §2.2's v0 header-capture rule
(find `<kw> <name>`, then consume header lines to the first proof-body
opener, blank-line-after-a-quote, or next command). `statement-equiv` is
`CheckClass.TRUST` on this prover — blocking, and isabelle's only
statement check — so an over-capturing header scan is a false block on a
merge gate and an unenumerated declaration is a statement nothing pins.
UNDETERMINED passes and defers to review.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

from gate.provers.base import CommentSyntax, ProverProfile, TrustEntry
from gate.provers.decl_syntax import (
    decl_line_regex_for,
    decl_prefix_fragment,
    decl_target_fragment,
    qualify_by_scope,
)

# ---------------------------------------------------------------------------
# Statement extraction.
# ---------------------------------------------------------------------------

# Declaration keywords — module-level so `_KEYWORD` and the profile's
# `decl_keywords`/`statement_keywords` share one source of truth. Every
# isabelle decl kind has a statement `extract_isabelle_statement` can
# resolve, so `statement_keywords` is this same tuple rather than a
# narrower subset (unlike lean4).
#
# Derived from the live Isabelle2025-2 `Main` keyword table (259
# commands), not from documentation: every Isar command carries a
# KEYWORD KIND, and the kinds that introduce a named theory-level
# declaration are exactly this audit's question. The group labels below
# are those kinds. `tests/gate/provers/test_isabelle.py::
# test_live_isabelle_declaration_command_coverage_is_complete` re-runs
# that dump as a skip-if-absent live test, so an upstream Isabelle
# adding a command in one of those kinds fails a test rather than
# becoming a silent fail-open.
#
# Completeness matters in both directions: an unrecognised command
# produces no span, and `gate.verify.style.find_decl_spans` folds its
# lines into the PRECEDING declaration's span — so the swallowed
# declaration goes uncompared (fail-open) and editing any of its lines
# reports its untouched neighbour CHANGED (false block).
#
# GARBAGE NAMES, kept deliberately: `partial_function (tailrec) f`
# enumerates as `(tailrec)`, `specification (c) name:` as `(c)`,
# `quotient_definition "n :: T" is …` as `"n`. Those are true span
# boundaries with ugly keys, and a wrong boundary is worse than a wrong
# name — the key is computed identically on both sides, so a body
# rewrite is still caught under the ugly name. Costs: two of them
# sharing a key in one file collide into the ambiguous-duplicate branch,
# and `extract_isabelle_statement` cannot re-find a name containing `(`
# or `"` (its prefix regex ends in `\b`). No real Isar identifier starts
# with either character.
#
# DELIBERATELY NOT ADDED, so the list is not re-derived: registration
# commands (`copy_bnf`, `datatype_compat`, `bnf`, `functor`,
# `lift_bnf`), aliases (`alias`, `type_alias`), the `thy_goal`
# instantiation commands (`instance`, `interpretation`, `sublocale`, …)
# and the ~58 other `thy_decl` commands (`notation`, `declare`, `ML`,
# `setup`, …) — none introduces a new named object. `oracle` names an ML
# function, and design note 12 §3.2 deliberately does not chase isabelle
# trust at the ML text level. `termination` would actively harm:
# `termination f2` enumerates as `f2` and collides with the `function
# f2` it belongs to. The nine `thy_decl_block` commands are SCOPES,
# tracked by `qualify_isabelle_decl_names`; the two carrying assumptions
# under a name are in the tuple — see the note below it.
_DECL_KEYWORDS = (
    # thy_goal_stmt — complete (5/5)
    "lemma",
    "theorem",
    "corollary",
    "proposition",
    "schematic_goal",
    # thy_stmt — complete (1/1)
    "axiomatization",
    # thy_defn
    "definition",
    "abbreviation",
    "fun",
    "primrec",
    "primcorec",
    "inductive",
    "inductive_set",
    "coinductive",
    "coinductive_set",
    "datatype",
    "codatatype",
    "record",
    "type_synonym",
    "lemmas",
    "inductive_cases",
    "inductive_simps",
    "fun_cases",
    "partial_function",
    # thy_goal_defn
    "function",
    "primcorecursive",
    "typedef",
    "quotient_type",
    "quotient_definition",
    "lift_definition",
    "specification",
    # thy_decl — uninterpreted objects only
    "typedecl",
    "consts",
    # thy_decl_block — the two NAMED assumption-bearing scopes.
    # Everything about these two entries is unlike the rest of this
    # tuple, so read the block comment below before touching them.
    "locale",
    "class",
)

# WHY `locale` AND `class` ARE HERE, and why the other seven
# `thy_decl_block` commands are not.
#
# A `locale`/`class` header carries `assumes` clauses — hypotheses
# discharged into EVERY theorem written in that scope, the same trust
# surface as a rocq section `Hypothesis`. Nothing enumerated them, so
# nothing compared them and a worker could retype one freely.
#
# THE COMPARISON MODE IS STATEMENT-ONLY, deliberately: they are in this
# tuple (hence in `statement_keywords`) and NOT in
# `_DEFINITION_KEYWORDS`, so a span difference goes to
# `extract_isabelle_statement`, which stops at `begin` — and the header
# is what needs pinning. Whole-span comparison would be wrong rather
# than merely stricter: a scope's span runs to its first enumerated
# inner declaration, so it absorbs whatever `notation`/`declare`/`text`
# lines open the body. A locale has no body in the definitional sense —
# the `begin … end` contents are separate declarations, each already
# enumerated.
#
# `begin` is an exact terminator (a reserved Isar keyword, so outside
# quotes it can be nothing else; it is in `_STOP_TOKENS`) but it is
# OPTIONAL — `locale A = fixes x assumes "P x"` with no `begin` builds —
# and such a header ends only at "the next command", of which this
# enumeration knows ~30 of Isabelle's ~100. So a `begin`-less header
# followed with no blank line by an unenumerated command over-captures
# it — the same residual `typedecl`/`consts` already carry, in the same
# direction (a false CHANGED, never a bypass).
#
# NAMED HOLE: `context`, `experiment` and `notepad` also carry
# assumptions (`context fixes cx :: nat assumes cxpos: "cx > 0" begin`
# builds on Isabelle2025-2) but are ANONYMOUS — no name to enumerate or
# key a comparison on, so a name-based enumeration cannot reach them.
# The other four (`instantiation`, `overloading`, `bundle`,
# `open_bundle`) state no proposition. `classes` is not caught by
# mistake: the boundary regex requires whitespace after the keyword.

_KEYWORD = rf"(?:{'|'.join(_DECL_KEYWORDS)})"

# The definition-bearing subset of `statement_keywords`
# (`ProverProfile.definition_keywords`): kinds whose *body* is content,
# compared whole rather than statement-only unless the base body is
# still a placeholder. See `gate.provers.base.ProverProfile` for the
# proof-irrelevance argument.
#
# `typedecl` and `consts` have no body at all, so the placeholder escape
# can never fire for them and the effect is whole-declaration
# comparison. `lemmas`, `inductive_cases`, `inductive_simps` and
# `fun_cases` name a FACT whose content is the thing after the `=`/`:`;
# they are here as the conservative choice — it is Isabelle's own
# `thy_defn` classification and there is no statement/proof split to
# isolate, so the alternative compares nothing at all.
#
# `schematic_goal` is the non-obvious entry: a schematic goal's PROOF
# instantiates the schematic variables in its own statement, so the body
# co-determines the resulting theorem. Verified on Isabelle2025-2 with
# the statement text held byte-identical — `schematic_goal sg:
# "(?x::nat) \<le> 5"` proved `by (rule order.refl)` yields `5 \<le> 5`
# and a downstream `lemma "(5::nat) \<le> 5" by (rule sg)` compiles;
# changing ONLY the proof to `by (rule le0)` yields `0 \<le> 5` and that
# same downstream fails. Statement-only comparison would be a real
# bypass for this kind.
#
# That also fixes the boundary: `lemma`, `theorem`, `corollary` and
# `proposition` all REFUSE a schematic goal outright ("Illegal schematic
# goal statement", verified for each of the four), so they are genuinely
# proof-irrelevant and stay statement-compared — `schematic_goal` is the
# only Isar goal command whose proof can move its own statement.
#
# Evidence grade: every entry's syntax and name position is
# execution-verified (a theory using all of them builds against
# `HOL-Library`). The CATEGORY of the co-/set- duals and of the
# `thy_goal_defn` type/constant commands is argued from the verified
# `datatype`/`inductive`/`primrec`/`function` entries rather than
# separately demonstrated — safe, because definition-bearing is the
# stricter comparison.
_DEFINITION_KEYWORDS = (
    "schematic_goal",
    "axiomatization",
    # thy_defn
    "definition",
    "abbreviation",
    "fun",
    "primrec",
    "primcorec",
    "inductive",
    "inductive_set",
    "coinductive",
    "coinductive_set",
    "datatype",
    "codatatype",
    "record",
    "type_synonym",
    "lemmas",
    "inductive_cases",
    "inductive_simps",
    "fun_cases",
    "partial_function",
    # thy_goal_defn
    "function",
    "primcorecursive",
    "typedef",
    "quotient_type",
    "quotient_definition",
    "lift_definition",
    "specification",
    # thy_decl — uninterpreted objects, body-less in effect
    "typedecl",
    "consts",
)

# The token that starts a definition body
# (`ProverProfile.definition_body_separator`). `where` is not part of any
# Isar type expression, so truncating a captured statement at the first
# whole-word `where` yields exactly the declaration's signature — which
# is what `gate.verify.statement_immutability._mask_definition_body`
# needs when a definition published with an unfilled body is
# legitimately filled.
#
# There is deliberately NO `definition_placeholder_tokens` here.
# `undefined` is HOL's unspecified constant *and* ordinary HOL content
# (`HOL/Library/FuncSet.thy`'s `extensional` uses it in real
# mathematics, and `HOL/MicroJava` has definitions whose entire body is
# exactly `undefined` — rewriting one changes what downstream lemmas
# prove, verified on Isabelle2025-2), so neither "mentions the token"
# nor "is exactly the token" separates a blueprint hole from content.
# The field was removed rather than narrowed, so an isabelle definition
# fill reports `CHANGED`: a deliberate false block, adjudicated by
# stage-two review (`docs/agents/ORCHESTRATOR.md` § Stage two asks the
# question). `sorry`/`oops` stay in `placeholder_tokens` and still reach
# the escape, so a declaration stubbed with a *proof* placeholder is
# unaffected.
_DEFINITION_BODY_SEPARATOR = "where"

# Top-level commands that can never be part of a declaration's body, for
# `gate.verify.style._span_end`'s trailing-trim allowlist
# (`ProverProfile.non_body_commands` — read that field's note for the
# membership rule and for why this does not close the class).
# Over-attribution is the dominant false-block cause, and on a
# whole-span-compared declaration it false-blocks 100% of the time:
# `HOL/IMP/Star.thy`'s `lemmas star_induct` owned the following
# `declare star.refl[simp,intro]` line, so inserting a helper before
# that `declare` reported `star_induct` CHANGED with its own text
# byte-identical, both versions building under Isabelle2025-2.
#
# DERIVED, not curated by taste: Isabelle classifies every command in a
# theory header's `keywords` clause, and the kind answers exactly the
# question this allowlist asks. Entries were read out of `Pure/Pure.thy`
# plus the `keywords` blocks of the whole Isabelle2025-2 source tree
# (and `Pure/Thy/thy_header.ML`'s `bootstrap_keywords` for what Pure
# cannot declare in Isar because it is needed to parse a theory header),
# filtered to theory-level kinds, minus `_DECL_KEYWORDS` (those lines
# are declarations, not trailers — putting `definition` here would turn
# the name-on-the-next-line spelling into a trailer instead of fixing
# its enumeration) and minus the prover-blind core in
# `gate.verify.style._NON_BODY_TRAILER_RE`.
#
# THE EXCLUSIONS MATTER MORE THAN THE INCLUSIONS. `prf_*`/`qed*` kinds
# are proof body and must never appear here. `diag`/`document_body`
# commands are legal in *both* modes, so they are admitted one at a time
# and only when goal-free (`value`, `term`, `thm`, `typ`, `prop` take an
# explicit argument). The goal-CONSUMING diagnostics — `nitpick`,
# `refute`, `quickcheck`, `nunchaku`, `sledgehammer`, `try`,
# `solve_direct` — stay out: a line invoking one sits between an
# unproved goal and its `oops`, which makes it proof body.

_NON_BODY_COMMANDS = (
    # theory-level kinds (thy_decl / thy_defn / thy_stmt / thy_load /
    # thy_decl_block / thy_goal / thy_goal_stmt / thy_goal_defn /
    # thy_end / document_raw), minus `decl_keywords` and minus the
    # prover-blind core
    "ML_export",
    "ML_file",
    "ML_file_debug",
    "ML_file_no_debug",
    "ROOTS_file",
    "SML_export",
    "SML_file",
    "SML_file_debug",
    "SML_file_no_debug",
    "SML_import",
    "activate_lazy_type",
    "activate_lazy_types",
    "adhoc_overloading",
    "alias",
    "atom_decl",
    "attribute_setup",
    "bibtex_file",
    "bnf",
    "bnf_axiomatization",
    "boogie_file",
    "bundle",
    "code_datatype",
    "code_identifier",
    "code_lazy_type",
    "code_monad",
    "code_pred",
    "code_printing",
    "code_reflect",
    "code_reserved",
    "coinduction_upto",
    "context",
    "copy_bnf",
    "corec",
    "corecursive",
    "datatype_compat",
    "datatype_record",
    "deactivate_lazy_type",
    "deactivate_lazy_types",
    "declaration",
    "declare",
    "default_sort",
    "domain",
    "domain_isomorphism",
    "domaindef",
    "equivariance",
    "experiment",
    "export_code",
    "external_file",
    "extract",
    "extract_type",
    "fixrec",
    "free_constructors",
    "friend_of_corec",
    "generate_file",
    "global_interpretation",
    "global_test",
    "hide_class",
    "hide_const",
    "hide_fact",
    "hide_type",
    "import_const_map",
    "import_file",
    "import_tptp",
    "import_type_map",
    "instance",
    "instantiation",
    "interpretation",
    "judgment",
    "lift_bnf",
    "lifting_forget",
    "lifting_update",
    "local_setup",
    "local_test",
    "method",
    "method_setup",
    "named_theorems",
    "nitpick_params",
    "no_adhoc_overloading",
    "no_notation",
    "no_syntax",
    "no_translations",
    "no_type_notation",
    "nominal_datatype",
    "nominal_inductive",
    "nominal_inductive2",
    "nominal_primrec",
    "nonterminal",
    "notepad",
    "nunchaku_params",
    "open_bundle",
    "oracle",
    "overloading",
    "parametric_constant",
    "parse_ast_translation",
    "parse_translation",
    "print_ast_translation",
    "print_lazy_types",
    "print_translation",
    "quickcheck_generator",
    "quickcheck_params",
    "realizability",
    "realizers",
    "recdef",
    "refute_params",
    "rep_datatype",
    "setup",
    "setup_lifting",
    "simproc_setup",
    "sledgehammer_params",
    "statespace",
    "subclass",
    "sublocale",
    "syntax",
    "syntax_consts",
    "syntax_declaration",
    "syntax_types",
    "text_raw",
    "translations",
    "type_alias",
    "type_notation",
    "typed_print_translation",
    "unbundle",
    # Pure/Thy/thy_header.ML's bootstrap keywords: `ML` is thy_decl,
    # the heading family is document_heading, `text`/`txt` are
    # document_body
    "ML",
    "chapter",
    "subsection",
    "subsubsection",
    "paragraph",
    "subparagraph",
    "text",
    "txt",
    # Same rule, theories that indent their `keywords` clause. `def` is
    # the one theory-level command deliberately left out: it is
    # `thy_defn`, but only in `Pure/ex/Def.thy` (an example theory no
    # real project loads), and a bare three-letter word this scanner
    # would match at the start of any line.
    "case_of_simps",
    "chapter_definition",
    "cpodef",
    "functor",
    "lemmas_with",
    "old_rep_datatype",
    "pcpodef",
    "session",
    "simps_of_case",
    "spark_end",
    "spark_open",
    "spark_open_vcg",
    "spark_proof_functions",
    "spark_types",
    "spark_vc",
    "termination",
    "time_definition",
    "time_fun",
    "time_fun_0",
    "time_function",
    "time_partial_function",
    "unoverload_definition",
    # goal-FREE diagnostics (kind `diag`): each takes an explicit
    # argument, so it is written at theory level between
    # declarations
    "thm",
    "thm_deps",
    "thm_oracles",
    "value",
    "values",
    "term",
    "typ",
    "prop",
    "prf",
    "full_prf",
    "ML_val",
    "ML_command",
    "find_theorems",
    "find_consts",
    "find_unused_assms",
    "unused_thms",
    "code_thms",
    "code_deps",
    "class_deps",
    "locale_deps",
    "thy_deps",
    "print_theorems",
    "print_state",
    "test_code",
    # …and the rest of the `print_*` family plus the remaining
    # argument-taking diagnostics. Check this list before adding to it:
    # `nitpick`, `nunchaku`, `quickcheck`, `refute`, `sledgehammer`,
    # `solve_direct`, `try`, `try0`, `sketch`, `sketch_subgoals`,
    # `approximate`, `real_limit`, `real_expansion` each need the
    # current goal, so a line invoking one is proof body.
    "print_ML_antiquotations",
    "print_abbrevs",
    "print_antiquotations",
    "print_attributes",
    "print_bnfs",
    "print_bundles",
    "print_case_translations",
    "print_cases",
    "print_claset",
    "print_classes",
    "print_codeproc",
    "print_codesetup",
    "print_coercions",
    "print_commands",
    "print_context",
    "print_context_tracing",
    "print_definitions",
    "print_defn_rules",
    "print_facts",
    "print_induct_rules",
    "print_inductives",
    "print_interps",
    "print_locale",
    "print_locales",
    "print_methods",
    "print_options",
    "print_orders",
    "print_pack",
    "print_quot_maps",
    "print_quotconsts",
    "print_quotients",
    "print_quotientsQ3",
    "print_quotmapsQ3",
    "print_record",
    "print_rules",
    "print_simpset",
    "print_statement",
    "print_syntax",
    "print_tcset",
    "print_term_bindings",
    "print_test",
    "print_theory",
    "print_trans_rules",
    "smt_status",
    "spark_status",
    "values_prolog",
    "welcome",
    "help",
    "explore",
    "cartouche",
    "compile_generated_files",
    "export_generated_files",
    "scala_build_generated_files",
)

# The same vocabulary as a line matcher, for `extract_isabelle_statement`'s
# header scan. Longest-first so a command that is a prefix of another
# cannot shadow it; `\b`-terminated so the no-space spellings
# (`text\<open>…`) still resolve.
_NON_BODY_LINE_RE = re.compile(
    r"^\s*(?:"
    + "|".join(
        re.escape(command) + r"\b"
        for command in sorted(_NON_BODY_COMMANDS, key=len, reverse=True)
    )
    + r")"
)

# Real Isar *command* modifiers, verified against Isabelle/Pure's own
# source: `Pure/Isar/token.ML`'s `is_command_modifier` lists
# `private`/`qualified`, and `Pure/Isar/outer_syntax.ML`'s
# `before_command` parses an optional one in front of any toplevel
# command. Without them `private lemma foo: "P"` is invisible to
# enumeration. `attribute_syntax` stays `None` on this profile:
# Isabelle attaches attributes *after* the name (`lemma foo[simp]:`),
# which `gate.provers.decl_syntax.normalize_decl_name` already handles,
# so no bracket-prefix syntax applies here.
_DECL_MODIFIERS = ("private", "qualified")

# Threaded into `extract_isabelle_statement`'s own `prefix_re` as well:
# enumerating a `private lemma foo:` boundary and *locating* that same
# line when asked for `foo`'s statement are two call sites that shared
# the same first-token assumption.
_DECL_PREFIX = decl_prefix_fragment(_DECL_MODIFIERS, None)

# The TARGET SPECIFICATION (`ProverProfile.decl_target_syntax`): the
# optional group Isabelle allows BETWEEN a declaration keyword and its
# name, `lemma (in A) foo:` — the standard way to target a locale.
# Without it enumeration read the literal `(in` as the name and
# `extract_isabelle_statement` returned `None`, so a rewritten statement
# on any locale-targeted lemma reported `UNDETERMINED`, which passes.
#
# THE GRAMMAR, from Isabelle2025-2's own source (`Pure/Isar/parse.ML`):
#
#   val target = ($$$ "(" -- $$$ "in") |-- !!! (name_position --| $$$ ")")
#   val opt_target = Scan.option target
#
# `Pure/Isar/outer_syntax.ML`'s `local_theory_command` splices
# `Parse.opt_target` in front of *every* command registered through
# `Outer_Syntax.local_theory*`, so "which commands accept it" is "every
# `local_theory` command". Measured on Isabelle2025-2, the enumerated
# keywords that REJECT it are `axiomatization`, `consts`, `record` (its
# typespec parses first, so `(in` reads as a type-variable list) and
# `locale`/`class` (`thy_decl_block` commands, which take a binding
# directly).
#
# The group is applied uniformly across `_DECL_KEYWORDS` rather than
# per-keyword, on `decl_line_regex`'s own terms: an over-permissive
# prefix on a line that cannot compile changes nothing, whereas
# under-recognising a real targeted declaration is the fail-open this
# closes. It cannot misfire on a legal line of a rejecting command
# either — `in` is a reserved Isar keyword, so `record (in_list) t`
# lexes `in_list` as one identifier and `\bin\b` does not match.
#
# WHAT CAN APPEAR INSIDE: `Parse.name` is `short_ident || long_ident ||
# sym_ident || number || string`, and `(in A)`, `(in "A")`, `( in A )`,
# `(in-)` and `(in -)` all build — `-` being the *global* target. Hence
# `[^)\s]+` for the content (one whitespace-free token, quotes included)
# rather than an identifier class, and `\s*` after `in\b` so `(in-)`
# still matches.
#
# RESIDUAL, verified legal and deliberately left: the target and the
# name may sit on DIFFERENT lines (`lemma (in A)` / newline / `foo:`
# builds), and so may the keyword and the target. The first degrades to
# the old `(in` name — the fragment is emitted optionally precisely so
# the regex backtracks into that — and `decl_name_from`'s next-line
# resolution does NOT rescue it, because group 2 captures the spurious
# token `(in` so the keyword-ends-its-line alternative never fires. 74
# declarations across the distribution key as bare `(in`; 56 collide
# with a sibling in the same file (UNDETERMINED, which passes) and only
# 10 have a recoverable name. Recovering those 10 needs multi-line name
# capture in all three enumeration consumers plus a rule telling
# `use_fps: "…"` (a name) from `assumes "…"` (not one) — inventing Isar
# syntax on a surface where being wrong SUPPRESSES a declaration.
_DECL_TARGET = r"\(\s*in\b\s*[^)\s]+\s*\)"

# `extract_isabelle_statement` needs the same optional target group
# between keyword and name that `decl_line_regex` emits, in both its
# named and its anonymous branch. Hoisted because the fragment — and,
# for the anonymous branch, the whole pattern, which interpolates no
# name — is the same string on every call, and `decl_target_fragment`
# compiles a regex of its own to reject capturing groups.
_DECL_TARGET_FRAGMENT = decl_target_fragment(_DECL_TARGET)
_ANON_DECL_PREFIX_RE = re.compile(
    rf"(?m)^\s*{_DECL_PREFIX}{_KEYWORD}(?=\s|$){_DECL_TARGET_FRAGMENT}"
)

# Decorations Isabelle attaches DIRECTLY to a declaration keyword, with
# no whitespace between them (`ProverProfile.decl_keyword_suffix`). Each
# made `decl_line_regex` fail to match the line at all — its
# `(KEYWORD)\s+` needs whitespace right after the keyword — so the
# declaration was absent from every enumeration built on that regex and
# nothing pinned its statement. Three alternatives, with provenance:
#
# - `\<^marker>\<open>…\<close>`, the document marker
#   (`Doc/Isar_Ref/Document_Preparation.thy`'s grammar
#   `@{syntax_def marker}: '\<^marker>' @{syntax cartouche}`), and the
#   commonest of the three. Matched non-greedily to the first `\<close>`
#   on the same line: every marker in the distribution is single-line,
#   and `[^\n]` keeps the fragment line-local like the rest of this
#   scanner.
# - `%tag`, the document-antiquotation shorthand (`theorem%important`).
#   Isabelle also permits a space before it (`consts %quote f :: …`), so
#   the fragment allows leading whitespace — which is why this is a
#   keyword_suffix alternative rather than something `\s+` covers.
# - `(option, …)`, a parenthesised option group written with no space
#   (`lift_definition(code_dt) foo`). `(?!in\b)` keeps it from
#   swallowing the TARGET specification, which `_DECL_TARGET` handles
#   and `qualify_by_scope` reads as a scope key — a keyword_suffix match
#   there would silently lose the locale qualification.
#
# Not covered, recorded rather than left implicit: `lemma(in A) foo`
# (two lines in the distribution) writes the target with no space
# before it, and the `(?!in\b)` guard declines it — resolving that
# spelling would widen the pattern for two lines.
_DECL_KEYWORD_SUFFIX = (
    r"(?:\\<\^marker>\\<open>[^\n]*?\\<close>"
    r"|\s*%[A-Za-z][A-Za-z0-9_']*"
    r"|\((?!in\b)[^)\n]*\))"
)

# Type parameters Isabelle's type-declaring commands write BEFORE the
# name (`ProverProfile.decl_type_params_syntax`): `datatype ('a, 'b) t`,
# `datatype 'a tree`, `typedef (overloaded) ('a, 'b :: len) vec`. Not a
# fail-open — the declaration IS enumerated — but `(\S+)` captured
# `('a,` / `'a` / `(overloaded)` as the "name", so every same-keyword
# type declaration in one theory collided under a non-identifier key.
#
# Two alternatives, matched repeatedly by `decl_type_params_fragment` so
# the two-group `typedef (overloaded) ('a, 'b) vec` spelling resolves: a
# parenthesised group (which also fixes `primrec (nonexhaustive) f`) and
# a bare single type variable. Both are ordinary Isar; neither can match
# an identifier, a quoted goal (`"P x"`) or an attribute list
# (`[simp]:`), so a declaration written without parameters is
# byte-identical to before.
_DECL_TYPE_PARAMS = r"(?:\([^)\n]*\)|'[A-Za-z][A-Za-z0-9_']*)"

# The same target as a SCOPE KEY rather than a skipped group
# (`qualify_by_scope`'s `decl_target_re`). `lemma (in A) foo` is the same
# declaration, and resolves to the same name `A.foo`, as a `lemma foo`
# written inside `locale A … begin`, so keying it `A.foo` is what the
# scope-path machinery already computes for the block-written spelling.
# Group 1 does NOT participate for the global target `(in -)`, which is
# correct: `(in -)` produces a global result whatever context encloses
# it, so the bare surface name is right.
#
# Anchored on the same keyword/modifier alternation as `_DECL_LINE_RE`
# rather than searched loose, so a `(in …)` inside a *statement* cannot
# be mistaken for a target. Quotes are matched outside the group so
# `(in "A")` keys as `A`, agreeing with the unquoted spelling; a
# spelling neither alternative covers simply does not match, and
# `qualify_by_scope` falls back to the enclosing stack — a coarser key,
# never a wrong one.
_DECL_TARGET_KEY_RE = re.compile(
    rf"""^\s*{_DECL_PREFIX}{_KEYWORD}\s+"""
    rf"""\(\s*in\b\s*(?:-|"?([A-Za-z_][\w'.]*)"?)\s*\)"""
)

# First-token markers that end the declaration header and start (or
# abandon, or complete) the proof body. A blank line also ends the
# header, but only once at least one quoted segment has been seen — a
# blank line before any quote is tolerated as part of a multi-line
# long-goal header.
#
# `begin` is not a proof opener but it is an exact header terminator for
# the two scope commands in `_DECL_KEYWORDS`: a `locale`/`class` header
# ends at `begin`, whether that sits on the keyword's own line or
# several lines later. It is a reserved Isar keyword, so outside quotes
# it can be nothing else, and no other kind here reaches a `begin`
# before another terminator fires.
#
# WORD markers only; Isar's terminal `.` / `..` methods are punctuation
# and live in `_TERMINAL_PROOF_RE` below.
_STOP_TOKENS = frozenset(
    {
        "proof",
        "apply",
        "by",
        "using",
        "unfolding",
        "sorry",
        "oops",
        "done",
        "begin",
    }
)

# The same set as a whole-word regex, DERIVED from it rather than
# written out again — a second hand-maintained copy is the drift shape
# `gate/checks.py` exists because of. Used by
# `_truncate_unquoted_stop_token` to catch a same-line proof
# (`lemma foo: "..." sorry`), which the first-token check never sees
# because it does not fire on the keyword's own line.
_STOP_TOKEN_RE = re.compile(
    r"\b(?:" + "|".join(sorted(_STOP_TOKENS)) + r")\b"
)

# Isar's TERMINAL PROOF METHODS, `.` and `..`. Both complete a proof
# outright (`.` is `by this`, `..` is `by rule`), so `lemma foo: "P" ..`
# is a whole declaration and the `..` is proof text, not statement text.
# They cannot join a whole-word set, and leaving them out was a live
# false block on the check that blocks merges here: a base
# `lemma foo: "P" .` captured the `.` into its statement, so replacing
# it with `by simp` — a proof-only edit — reported CHANGED.
#
# `.` is NOT only a proof method, which is what makes this delicate: it
# also separates qualified names (`Nat.add`), and appears inside quoted
# terms (`\<forall>x. P x`, HOL's `{0..n}`). A scan cutting at any `.`
# would UNDER-capture, i.e. bypass. Three conditions prevent that, and
# the first two are why this is a separate regex rather than another
# `_STOP_TOKEN_RE` alternative:
#
# 1. applied only OUTSIDE quotes and cartouches, by the same scan the
#    word tokens use (`_truncate_unquoted_stop_token`);
# 2. the run of dots must be the LAST thing on the line — that is what
#    separates a terminal method from `Nat.add`, whose dot is followed
#    by `add`;
# 3. the run must be exactly one or two dots, so `...` (not Isar) is
#    left alone. `(?<!\.)` is what makes that true rather than nearly
#    true: the scan walks left to right, so without it the pattern
#    matches the last two dots of a longer run. Python's
#    `Pattern.match(s, pos)` lets a lookbehind read before `pos`,
#    unlike `^`, so this works mid-scan.
#
# Whitespace *before* the dot is deliberately NOT required, because
# Isabelle does not require it: `lemma dd_nospace: "True"..` BUILDS, and
# for `lemma singledot: "(1::nat) = 1".` the toolchain reports `Failed
# to finish proof … At command "."` — naming `.` as the command, so the
# token is the terminal method there and only the proof is wrong.
_TERMINAL_PROOF_RE = re.compile(r"(?<!\.)\.\.?[ \t]*$")

# Cartouche delimiters, tracked alongside quote parity by
# `_truncate_unquoted_stop_token`. `\<open>…\<close>` is the modern Isar
# alternative to `"…"` for a statement, and HOL's interval notation puts
# a literal `..` inside one (`\<open>x \<in> {0..n}\<close>`), so a `..`
# rule ignoring cartouches could truncate a real statement. Tracking
# them also stops a stop-token WORD inside a cartouche from truncating.
#
# The ASCII spelling is the one to track: the Isabelle2025-2
# distribution's theories are written `\<open>`/`\<close>` and the
# Unicode `‹›` form does not appear — treating `›` as a closer would end
# cartouche state early on legal informal text.
_CARTOUCHE_OPEN = "\\<open>"
_CARTOUCHE_CLOSE = "\\<close>"
_COMMENT_MARK = "\\<comment>"


def _scan_line(
    line: str, in_quote: bool, cartouche_depth: int, *, cut: bool
) -> tuple[int | None, bool, int]:
    r"""Walk one line, carrying quote/cartouche state; optionally find a cut.

    The single state machine behind both of this module's line scans, so
    one place knows what an Isar statement's delimiters are:

    - `cut=True` (`_truncate_unquoted_stop_token`) stops at the first
      stop token outside a statement and returns its index;
    - `cut=False` (`isabelle_continuation_lines`) never stops, and the
      returned state is the state at end of line.

    Returns `(cut_index_or_None, in_quote, cartouche_depth)`.

    Quotes and cartouches do not nest inside each other: within a `"…"`
    a `\<open>` is ordinary text, and within a cartouche a `"` is. That
    matches Isabelle's own lexer, where the two are distinct token
    types, and it is load-bearing — Isabelle prose is full of unbalanced
    quotes inside cartouches (`subsubsection\<open>… All Parts" of a
    Message\<close>`), and a parity rule counting that `"` would read
    the rest of the file as one open statement. Cartouche depth is
    clamped at zero so a stray `\<close>` cannot make it negative.

    **Backslash escapes inside a quote ARE modelled.** Isabelle's own
    string scanner (`Pure/General/symbol_pos.ML`, `scan_str`) accepts
    `\` followed by the quote, another `\`, or a char code, so `\"` is a
    legal escape and a *buildable* theory can carry an odd number of raw
    `"` on one line (`HOL/HOL.thy`'s `code_printing` blocks do).
    Without this, such a line flips quote parity permanently and every
    declaration after it is read as a continuation line and suppressed —
    an unbounded fail-open. A `\` that is really the head of a `\<name>`
    symbol consumes the `<` harmlessly: the only character this scan
    looks for is `"`, and a symbol never ends in `\`.
    """
    i = 0
    n = len(line)
    while i < n:
        if in_quote:
            if line[i] == "\\" and i + 1 < n:
                i += 2
                continue
            if line[i] == '"':
                in_quote = False
            i += 1
            continue
        if cartouche_depth:
            if line.startswith(_CARTOUCHE_OPEN, i):
                cartouche_depth += 1
                i += len(_CARTOUCHE_OPEN)
                continue
            if line.startswith(_CARTOUCHE_CLOSE, i):
                cartouche_depth -= 1
                i += len(_CARTOUCHE_CLOSE)
                continue
            i += 1
            continue
        if line[i] == '"':
            in_quote = True
            i += 1
            continue
        if line.startswith(_CARTOUCHE_OPEN, i):
            cartouche_depth += 1
            i += len(_CARTOUCHE_OPEN)
            continue
        if line.startswith(_CARTOUCHE_CLOSE, i):
            # A closer with no opener seen: swallow it rather than going
            # negative.
            i += len(_CARTOUCHE_CLOSE)
            continue
        if cut and (
            _STOP_TOKEN_RE.match(line, i) is not None
            or _TERMINAL_PROOF_RE.match(line, i) is not None
        ):
            return i, in_quote, cartouche_depth
        i += 1
    return None, in_quote, cartouche_depth


def isabelle_continuation_lines(lines: Sequence[str]) -> frozenset[int]:
    r"""0-based indices of lines that BEGIN inside an open statement or cartouche.

    `ProverProfile.decl_continuation_lines`. Every enumeration built on
    `decl_line_regex` matches line by line with no memory, and on
    isabelle that mis-splits a legitimate multi-line quoted header —
    this BUILDS:

        definition ff :: "(nat, nat)
          fun " where "ff = (\<lambda>n. n)"

    `fun` there is HOL's function-type constructor written postfix
    inside an *open* quoted type, not a command, so the second line
    matched as a declaration named `"`: `ff`'s span stopped at its first
    line (fail-open) and a phantom declaration appeared beside it.

    **Cartouches count too**: a line inside a `text \<open>…\<close>`
    block is prose, and Isabelle prose routinely starts a line with a
    word like "definition" or "lemma".

    **Feed this the comment-blanked lines**, the same copy the consumers
    match declarations against.

    Failure direction: a mis-read state SUPPRESSES declaration lines,
    which is a fail-open, so this is the one place in this module where
    being wrong is worse than being coarse. It reuses `_scan_line`
    rather than a parity count — a parity count lost real declarations
    to a single unbalanced `"` inside a prose cartouche — and
    `_scan_line` models the two things that used to make its state run
    away: `(*` is inert inside a cartouche
    (`gate.inventory.scan.strip_comments`' `verbatim_delimiters`) and
    `\"` is an escape rather than a closing quote.

    **There is no blank-line reset, and do not reinstate one as a
    "safety net".** It is not one: a blanked comment line looks blank,
    so resetting there cleared the cartouche state of `ML \<open>` /
    `text \<open>` blocks and every SML `fun` or line of English inside
    them enumerated as a phantom declaration — and a phantom is a
    fail-open too, colliding with a real name into `UNDETERMINED` (which
    passes) and truncating the preceding real declaration's span. If a
    new runaway shape turns up, fix the state machine.
    """
    out: set[int] = set()
    in_quote = False
    depth = 0
    for index, line in enumerate(lines):
        if not line.strip():
            # A blank line carries the state through rather than
            # clearing it — a cartouche or a quoted statement is not
            # closed by a paragraph break. It is still not reported as a
            # continuation line, because no consumer can read a
            # declaration off an empty line anyway.
            continue
        if in_quote or depth:
            out.add(index)
        _cut, in_quote, depth = _scan_line(line, in_quote, depth, cut=False)
    return frozenset(out)


def _truncate_unquoted_stop_token(
    line: str, in_quote: bool = False, cartouche_depth: int = 0
) -> tuple[str, bool, int]:
    r"""Cut `line` at the first stop token outside quotes; carry the state.

    Isar statements live inside `"…"` quotes or `\<open>…\<close>`
    cartouches, either of which may contain a stop-token word as
    ordinary statement text (`"a by b"`) or a literal `..` (HOL's
    interval notation). This scans left to right tracking quote parity
    and cartouche depth, so a same-line proof tacked on after the
    statement gets cut while the same text inside the statement is left
    untouched. Two token classes, needing different rules: the WORD
    markers of `_STOP_TOKENS`, matched anywhere on the line, and Isar's
    terminal `.` / `..`, matched only at end of line — see
    `_TERMINAL_PROOF_RE` for why a looser rule there would be a bypass.

    `in_quote` and `cartouche_depth` are the state carried in from the
    PRECEDING captured line, and the returned pair is the state carried
    out, so the caller can apply this to every line of a multi-line
    header rather than only the last. Starting each line fresh would
    read a continuation line of an open statement as unquoted text and
    truncate real content.

    The returned state is meaningful only when nothing was truncated — a
    truncated line ends the header, so the caller stops reading.

    The walk itself is `_scan_line`, shared with
    `isabelle_continuation_lines`.
    """
    index, in_quote, cartouche_depth = _scan_line(
        line, in_quote, cartouche_depth, cut=True
    )
    if index is None:
        return line, in_quote, cartouche_depth
    return line[:index], in_quote, cartouche_depth


# Isabelle comments must not be read as part of a statement: a marginal
# `\<comment> \<open>…\<close>` cartouche or a `(* … *)` block placed
# between `shows "…"` and the proof would otherwise make statement-equiv
# see a change with the logical statement unchanged.
# `_blank_isabelle_comments` removes both.
#
# _BLANKER_DUPLICATION — why Choir has TWO comment blankers.
# `gate.inventory.scan.strip_comments` is the generic every-prover one
# (five consumers); `_blank_isabelle_comments` serves
# `extract_isabelle_statement` alone. They agree on every shape tried
# except Isabelle's marginal comment `\<comment> \<open>…\<close>`, which
# this one blanks and the generic one leaves visible — pinned as the only
# difference by `tests/gate/provers/test_isabelle.py::
# test_r9_the_two_blankers_agree_except_on_marginal_comments`.
#
# They were kept separate on a measurement: unifying them gained 2 real
# names and lost 15 declarations whose name sits on the line *after* the
# comment (`consts \<comment> \<open>…\<close>` then `initState :: …`),
# because blanking left the keyword alone on its line and the per-line
# keyword-then-name match found no name. **That mechanism no longer
# holds** — `decl_line_regex` now matches a keyword that ends its line
# and `decl_name_from` resolves the name from the following lines, so on
# that exact shape `find_decl_spans` recovers `initState` with the span
# unchanged instead of losing it. The loss side of the trade has to be
# re-measured over the distribution before merging them or citing the
# old numbers; nothing here has re-run it.
#
# The cost of not unifying, stated: on isabelle the generic blanker
# leaves a marginal comment's text visible, so `\<comment> \<open>…the
# earlier sorry\<close>` counts as a placeholder occurrence in the
# blocking `sorry-delta` check. No instance exists in the distribution,
# so this is an unrealised false-block risk, not a fail-open.


def _string_literal_end(text: str, start: int) -> int:
    r"""Index just past the `"…"` literal opening at `start`.

    Backslash escapes are consumed in pairs, so an escaped quote does
    not close the literal early — matching Isabelle's own `scan_str`
    (`Pure/General/symbol_pos.ML`), where `\` may only precede the
    quote, another `\`, or a char code. `_scan_line` models the same
    rule.
    """
    n = len(text)
    i = start + 1
    while i < n and text[i] != '"':
        i += 2 if (text[i] == "\\" and i + 1 < n) else 1
    return i + 1


def _block_comment_end(text: str, start: int) -> int:
    r"""Index just past the nested `(* … *)` comment opening at `start`.

    Isabelle's source comments nest, and only their own delimiters are
    significant inside them — a `\<open>` in a comment is ordinary text,
    verified against Isabelle2025-2's build. An unterminated comment
    runs to end of text.
    """
    n = len(text)
    depth, i = 1, start + 2
    while i < n and depth > 0:
        if text.startswith("(*", i):
            depth, i = depth + 1, i + 2
        elif text.startswith("*)", i):
            depth, i = depth - 1, i + 2
        else:
            i += 1
    return i


def _marginal_comment_end(text: str, start: int) -> int | None:
    r"""Index just past a `\<comment> \<open>…\<close>` at `start`, else `None`.

    `start` points at the `\<comment>` mark. `None` means the mark is
    not followed (modulo whitespace) by a cartouche, so it is not a
    marginal comment and the caller should treat it as ordinary text.
    The body's own nested cartouches are counted, so the span consumed
    is balanced.
    """
    n = len(text)
    i = start + len(_COMMENT_MARK)
    while i < n and text[i] in " \t\r\n":
        i += 1
    if not text.startswith(_CARTOUCHE_OPEN, i):
        return None
    depth = 1
    i += len(_CARTOUCHE_OPEN)
    while i < n and depth > 0:
        if text.startswith(_CARTOUCHE_OPEN, i):
            depth, i = depth + 1, i + len(_CARTOUCHE_OPEN)
        elif text.startswith(_CARTOUCHE_CLOSE, i):
            depth, i = depth - 1, i + len(_CARTOUCHE_CLOSE)
        else:
            i += 1
    return i


def _blank_isabelle_comments(text: str) -> str:
    r"""Blank Isabelle comment spans with spaces, preserving newlines.

    Handles nested `(* … *)` blocks and marginal
    `\<comment> \<open>…\<close>` cartouche comments (whose body may
    itself contain nested cartouches), and skips `"…"` string literals
    so a comment marker inside a statement is not mistaken for one.

    **Cartouche depth is tracked, so a `(*` inside a cartouche is not a
    comment opener.** `\<open>(*)\<close>` — HOL's multiplication
    operator, `HOL/Library/Word.thy` — otherwise opens a comment that
    never closes and blanks the rest of the file, which on isabelle
    makes the blocking `statement-equiv` report `UNDETERMINED` (i.e.
    pass) for a rewritten statement. A `"` inside a cartouche is
    likewise ordinary text. Both precedence rules were verified against
    Isabelle2025-2's own build in both directions: a cartouche opener
    inside a `(* … *)` is inert, and a comment opener inside a cartouche
    is inert even unbalanced (`\<^verbatim>\<open>(*\<close>`).

    Marginal `\<comment>` comments are recognized **at every cartouche
    depth**, deliberately: Isabelle's inner syntax has formal comments
    too, so `lemma foo: \<open>P \<comment> \<open>note\<close>\<close>`
    is a statement carrying a comment (it builds) and blanking it keeps
    annotating a statement from reading as a statement change. The
    consumed span is balanced, so the depth counter stays consistent.

    Same rule as `gate.inventory.scan.strip_comments`'
    `verbatim_delimiters`, which is where the generic copy lives; the
    two are deliberately not merged — see `_BLANKER_DUPLICATION`.
    """
    out = list(text)
    n = len(text)

    def blank(a: int, b: int) -> None:
        for j in range(a, b):
            if out[j] != "\n":
                out[j] = " "

    i = 0
    cartouche_depth = 0
    while i < n:
        # Checked first, and so at every cartouche depth — see the
        # docstring's paragraph on why inner-syntax marginal comments
        # stay recognized inside a statement cartouche. A `\<comment>`
        # inside a `"…"` literal is still never reached, because the
        # literal is skipped whole.
        marginal = (
            _marginal_comment_end(text, i)
            if text.startswith(_COMMENT_MARK, i)
            else None
        )
        if marginal is not None:
            blank(i, marginal)
            i = marginal
        elif cartouche_depth:
            if text.startswith(_CARTOUCHE_OPEN, i):
                cartouche_depth += 1
                i += len(_CARTOUCHE_OPEN)
            elif text.startswith(_CARTOUCHE_CLOSE, i):
                cartouche_depth -= 1
                i += len(_CARTOUCHE_CLOSE)
            else:
                i += 1
        elif text[i] == '"':  # string literal — skip to the closing quote
            i = _string_literal_end(text, i)
        elif text.startswith("(*", i):  # nested block comment
            end = _block_comment_end(text, i)
            blank(i, end)
            i = end
        elif text.startswith(_CARTOUCHE_OPEN, i):
            cartouche_depth = 1
            i += len(_CARTOUCHE_OPEN)
        elif text.startswith(_CARTOUCHE_CLOSE, i):
            # A closer with no opener: swallow it rather than going
            # negative (an unbalanced cartouche does not parse).
            i += len(_CARTOUCHE_CLOSE)
        else:
            i += 1
    return "".join(out)


def extract_isabelle_statement(text: str, decl_name: str) -> str | None:
    r"""Return the header text (keyword through the statement) for `decl_name`.

    Looks for `<kw> <name>` at the start of a line (MULTILINE), then
    accumulates lines until any of these terminators:

    - a line whose first token is a proof-body opener (`_STOP_TOKENS`);
    - a blank line, once at least one quoted segment has been captured;
    - an unquoted stop-token word *within* a captured line, truncated
      off — this is what catches a same-line proof (`lemma foo: "..."
      sorry`), which the first-token check never sees;
    - Isar's terminal proof methods `.` / `..` at end of a captured line
      (`_TERMINAL_PROOF_RE`), likewise truncated off and likewise
      invisible to a whole-word check;
    - **the next declaration** (`_DECL_LINE_RE`);
    - **the next non-declaration top-level command**
      (`_NON_BODY_LINE_RE`) — `oracle`, `declare`, `text`, `instance`, …
      Also the invariant `tests/gate/provers/test_isabelle.py::
      test_no_extraction_reaches_outside_its_own_span` pins: the same
      vocabulary shrinks `find_decl_spans`' spans, so without this the
      whole-file and span-scoped extractions disagree.

      That last one is gated on **indent** as well as quote state, and
      the guard is load-bearing: a declaration whose *name* happens to
      be one of these command words, written on the line after its
      keyword (`definition` / `  extract :: nat where …` — `extract` is
      a real `thy_decl` in `HOL/Proofs/Extraction`), would otherwise
      terminate the scan on its own name and return a statement with no
      statement in it. Requiring the line to sit at or left of the
      declaration line's column mirrors
      `gate.verify.style._span_end`'s body rule, so the two stay in
      step by construction.

    Lines are joined and whitespace runs collapsed to single spaces.

    Without the last two terminators, two very ordinary shapes ran
    straight into whatever command followed — a proof-less command
    (`definition`, `consts`, `typedecl`, …) reaches no stop token at
    all, and a same-line proof puts its stop token on the keyword's own
    line, which the first-token check skips — so the captured
    "statement" contained a neighbouring declaration and editing that
    neighbour made this declaration's statement differ. On isabelle
    `statement-equiv` is `CheckClass.TRUST`, so that was a live false
    block naming a declaration nobody had touched.

    The declaration terminator is gated on quote parity carried across
    lines, because a legitimate multi-line header may continue with a
    line whose first token *is* a declaration keyword: `definition ff ::
    "(nat, nat)\n  fun " where …` builds, and `fun` there is the
    function-type constructor inside an open quoted type. Header
    continuations that are not inside quotes (`fixes` / `assumes` /
    `shows` / `for` / `and` / `|`) are not declaration keywords, so they
    are unaffected.

    Returns `None` if no match found. Isabelle comments are blanked
    before capture, so annotating near a statement does not trip
    statement-equiv. Attribute annotations (`[simp]` etc.) are still
    naive; planned iteration, same maturity as lean4's v0 extractor.
    Leading `private`/`qualified` command modifiers are skipped via
    `_DECL_PREFIX` before the keyword is matched.
    """
    text = _blank_isabelle_comments(text)
    if decl_name:
        prefix_re = re.compile(
            rf"(?m)^\s*{_DECL_PREFIX}{_KEYWORD}\s+"
            rf"{_DECL_TARGET_FRAGMENT}{re.escape(decl_name)}\b"
        )
    else:
        # An ANONYMOUS declaration: `lemma "P x"`, `lemma [simp]: "P"`,
        # or a long goal whose own line ends after the keyword. There is
        # no name to anchor on, so anchor on the keyword and let the
        # terminator scan below find the statement's end exactly as it
        # does for a named one.
        #
        # Safe only because callers pass a *single declaration's span
        # text*: "the first declaration in this text" is then the one
        # being asked about. A whole-file caller would get the file's
        # first declaration instead, which is why the empty name is a
        # deliberate contract and not a fallback for a name that failed
        # to parse. Without this the span scanner reports such
        # declarations under a non-name token (`"P`, `[simp]:`), two in
        # one file collide on it, and filling one's proof reports
        # UNDETERMINED — a false red on permitted work.
        prefix_re = _ANON_DECL_PREFIX_RE
    match = prefix_re.search(text)
    if match is None:
        return None

    lines = text[match.start() :].splitlines()
    captured: list[str] = []
    quoted_seen = False
    in_quote = False
    cartouche_depth = 0
    # `^\s*` in `prefix_re` can consume the newline(s) of preceding blank
    # lines, so `lines[0]` is not necessarily the declaration's own line.
    # The declaration terminator must not fire on the declaration itself,
    # and `captured` being non-empty does not mean the keyword line has
    # been read yet — hence an explicit flag rather than `if captured`.
    decl_line_seen = False
    decl_indent = 0
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if quoted_seen:
                break
            captured.append(line)
            continue
        if decl_line_seen and not in_quote and _DECL_LINE_RE.match(line) is not None:
            break
        if (
            decl_line_seen
            and not in_quote
            and cartouche_depth == 0
            and len(line) - len(line.lstrip()) <= decl_indent
            and _NON_BODY_LINE_RE.match(line) is not None
        ):
            break
        first_token = stripped.split(None, 1)[0]
        if decl_line_seen and first_token in _STOP_TOKENS:
            break
        truncated, in_quote, cartouche_depth = _truncate_unquoted_stop_token(
            line, in_quote, cartouche_depth
        )
        captured.append(truncated)
        if not decl_line_seen:
            decl_indent = len(line) - len(line.lstrip())
        decl_line_seen = True
        if '"' in line:
            quoted_seen = True
        if truncated != line:
            break

    joined = " ".join(captured)
    return re.sub(r"\s+", " ", joined).strip()


# ---------------------------------------------------------------------------
# Scope-path disambiguation key
# ---------------------------------------------------------------------------

# `locale A = B + C`, `class C = ord + …` and `context A` all scope the
# declarations inside their `begin` … `end` block and all contribute
# their name to the disambiguation path — a `lemma c` inside `locale A`
# is reachable as `A.c` in real Isabelle, so the key usually coincides
# with the resolved name. It is still only a disambiguation key: locale
# *interpretation* and `sublocale` re-export facts under other names,
# which this ignores by design (see
# `gate.provers.decl_syntax.qualify_by_scope`).
#
# The `(?!begin\b)` guard matters: `qualify_by_scope` tries this pattern
# before the anonymous one, so without it `context begin` would push the
# literal word `begin` as a contributing scope name. `class` is here
# because it was previously tracked by nothing, so its closing `end`
# popped whatever `locale` happened to enclose it. `classes` cannot
# match — the `\s+` requires whitespace after the keyword.
_LOCALE_OPEN_RE = re.compile(
    r"^\s*(?:locale|class|context)\s+(?!begin\b)([A-Za-z_][\w'.]*)"
)

# An anonymous `context begin … end` block leaves its declarations
# global, so it contributes nothing — but it must still be tracked, or
# its `end` pops an enclosing `locale`. Same role as rocq's `Section`
# and lean4's `section`. Written to require `begin`, since a bare
# `context` line with a name is the case above.
_ANON_CONTEXT_OPEN_RE = re.compile(r"^\s*context\s+begin\b")

# The scope only opens at `begin`, which may sit on the `locale` line
# itself or several lines later (an Isar locale header can run over
# `fixes`/`assumes` lines), so the opener above ARMS a pending name and
# this commits it — `qualify_by_scope`'s `commit_re`. Searched rather
# than matched at line start, because `locale A = B begin` puts it
# mid-line.
_BEGIN_RE = re.compile(r"\bbegin\b")

# Isabelle's `end` takes no argument, so this closer names no scope and
# `qualify_by_scope` pops the innermost entry. A theory file's own
# `theory … begin` / `end` wrapper is deliberately NOT tracked as an
# opener: `theory` is not a locale, so its closing `end` pops an empty
# stack, which is a no-op.
_END_RE = re.compile(r"^\s*end\s*$")


def qualify_isabelle_decl_names(text: str) -> dict[int, str]:
    """Enclosing-`locale` path per declaration line — a DISAMBIGUATION key.

    Not name resolution: see `gate.provers.decl_syntax.qualify_by_scope`
    for the argument that this only has to be stable, structure-derived
    and computed identically on both sides, which is what makes it
    implementable without resolving locale interpretation or
    `sublocale`.

    What it fixes: two `lemma c`s in sibling `locale`s used to group
    under the bare surface name `c`, so filling one's proof made the
    per-name text multiset differ while the name was not unique on
    either side — the ambiguous-duplicate branch fired on an entirely
    permitted edit and reported `UNDETERMINED`.

    Known-imprecise, and harmless for the reason above: isabelle has
    other `begin` … `end` constructs this does not open
    (`instantiation`, `overloading`, `bundle`, `notepad`), so one of
    their `end`s can pop a `locale` it did not open and under-qualify
    every declaration after it. That happens identically on both sides,
    so keys still match; the cost is a collision this could have
    resolved, which is the `UNDETERMINED` those files already got.
    """
    return qualify_by_scope(
        text,
        decl_re=_DECL_LINE_RE,
        qualifying_open_re=_LOCALE_OPEN_RE,
        plain_open_re=_ANON_CONTEXT_OPEN_RE,
        close_re=_END_RE,
        commit_re=_BEGIN_RE,
        decl_target_re=_DECL_TARGET_KEY_RE,
        continuation_lines=isabelle_continuation_lines,
    )


# ---------------------------------------------------------------------------
# Trust-report hooks (design note 12 §4).
#
# VALIDATED against a live Isabelle2025-2 install. `isabelle process` is
# NOT a tool that exists (`isabelle` with no arguments lists
# `ML_process`, `console` and `process_theories`), and the entry point
# is `Thm_Deps.all_oracles : thm list -> Proofterm.oracle list`
# (`Pure/thm_deps.ML`) — not `Proofterm.all_oracles_of`, which does not
# exist. `Thm_Deps.all_oracles` is what the `thm_oracles` Isar command
# itself calls.
#
# Three design calls behind the invocation, each made against measured
# behaviour:
#
# 1. **`imports` are theory names, not a session name.** Passing one to
#    `-l` answers `*** Undefined session(s)`, and the auto-detection
#    caller has no session to give — `changed_decls._module_for`
#    derives a *theory* stem from the changed file's path. Loading from
#    source also needs no heap image, which matters because the shipped
#    isabelle workflow builds with `isabelle build -D .` (no `-b`), so
#    none exists. Cost: re-elaborating the imports per probe; the heap
#    variant (`ML_process -d . -l <session>`) is verified working if a
#    session name ever reaches this hook.
# 2. **`-o quick_and_dirty`.** Without it a theory containing `sorry`
#    does not load at all ("Cheating requires quick_and_dirty mode!"),
#    so the one thing oracle tracking exists to report would be an
#    unresolvable probe. With it, `sorry` reports as the
#    `Pure.skip_proof` oracle. The flag affects this informational
#    probe only; the blocking rebuild runs its own `isabelle build`.
# 3. **`-o show_results=false` plus a `choir-trust:` line tag.**
#    Isabelle echoes each loaded theorem as `theorem name: prop`, one
#    pretty-printing line break away from a bare `<name>: <oracles>`
#    parser reading `foo:: "nat"` as a report line.
#
# ORACLES ONLY, deliberately. `Thm_Deps.all_oracles` reports oracles,
# which design note 12 §3.2 identifies as isabelle's sound detector (an
# Isar `sorry` is an oracle call, not a kernel axiom the way lean4's
# `sorryAx` is); an `axiomatization` is caught by this profile's
# text-scan `trust_patterns`. The axiom half IS reachable
# (`Thm_Deps.thm_deps` filtered by `Theory.all_axioms_of` correctly
# reported a project `axiomatization` on the live install) but the same
# run reported the base logic's own arity and `eq_reflection` axioms for
# entirely honest lemmas, on every declaration. Subtracting the base
# logic's axiom set needs a way to identify the base logic this hook
# does not have; recorded as the follow-up rather than guessed at.
#
# STILL NOT validated: the shipped workflow template
# (`scripts/new-project.sh`'s isabelle `verify-trust-report.yml` has
# never run — no isabelle project exists yet). What WAS validated on a
# real Isabelle2025-2 with only the bundled HOL heap:
# `collect_trust_report` directly, and `trust_report_cli`'s
# `--base-sha` auto mode against a git repository — both pinned as
# skip-if-absent live tests in `tests/gate/provers/test_trust.py`, the
# only kind of test that could have caught a nonexistent tool name.
# ---------------------------------------------------------------------------

# Every probe line is tagged, so no Isabelle banner, loading message or
# echoed theorem can be mistaken for one — see design call 3 above.
_TRUST_PROBE_TAG = "choir-trust:"

_TRUST_REPORT_LINE_RE = re.compile(
    rf"^{re.escape(_TRUST_PROBE_TAG)}(?P<thm>[^:]+):(?P<oracles>.*)$"
)


def _ml_string(value: str) -> str:
    """Render `value` as an SML string literal."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def build_isabelle_trust_ml(targets: list[str], imports: list[str]) -> str:
    """Return the ML snippet passed to `isabelle ML_process -e`.

    `imports` are theory names, loaded from source with
    `Thy_Info.use_theories` under the qualifier `Choir` (omitted
    entirely when there are none). Each target is then resolved against
    the loaded theories, most recent first — so the caller does not have
    to say which theory a fact lives in — and printed as one
    `choir-trust:<name>:<oracle,...>` line, empty after the final colon
    meaning no oracles. An unresolvable target raises, which
    `gate.provers.trust.collect_trust_report` turns into a
    `ProverError`; in auto mode that is one `unresolved` report line,
    exactly as for lean4.
    """
    lines = [
        "let",
        "  fun oracle_names thm =",
        "    Thm_Deps.all_oracles [thm] |> map (fn ((name, _), _) => name);",
        "  fun in_theory name thy_name =",
        "    (let val ctxt = Proof_Context.init_global (Thy_Info.get_theory thy_name)",
        "     in SOME (Proof_Context.get_thm ctxt name) end)",
        "    handle ERROR _ => NONE;",
        "  fun resolve name = get_first (in_theory name) (rev (Thy_Info.get_names ()));",
        "  fun report name =",
        "    (case resolve name of",
        "      SOME thm =>",
        f"        writeln ({_ml_string(_TRUST_PROBE_TAG)} ^ name ^ \":\" ^"
        " String.concatWith \",\" (oracle_names thm))",
        '    | NONE => error ("choir trust probe: unresolved fact " ^ name));',
        "in",
    ]
    body: list[str] = []
    if imports:
        theories = ", ".join(
            f"({_ml_string(theory)}, Position.none)" for theory in imports
        )
        body.append(
            "  Thy_Info.use_theories (Options.default ()) \"Choir\" "
            f"[{theories}]"
        )
    body.extend(f"  report {_ml_string(target)}" for target in targets)
    lines.append(";\n".join(body))
    lines.append("end")
    return "\n".join(lines)


def isabelle_trust_report_command(
    workspace: Path, targets: list[str], imports: list[str]
) -> list[str]:
    """Build the `isabelle ML_process` invocation (see the caveats above).

    `workspace` is unused: `gate.provers.trust.collect_trust_report`
    runs the argv with `cwd=workspace`, so the `-d .` session-directory
    flag already points at it — kept in the signature because
    `ProverProfile.trust_report_command` is one shared shape across
    profiles, and lean4/rocq use it to write a probe file.
    """
    del workspace  # see docstring
    return [
        "isabelle",
        "ML_process",
        "-d",
        ".",
        "-o",
        "quick_and_dirty",
        "-o",
        "show_results=false",
        "-e",
        build_isabelle_trust_ml(targets, imports),
    ]


def parse_isabelle_trust_report(output: str) -> list[TrustEntry]:
    """Parse `choir-trust:<thm>:<oracle1>,<oracle2>` lines into `TrustEntry`.

    An empty oracle list after the second colon means clean. Every line
    that does not carry the `choir-trust:` tag is ignored — Isabelle's
    own banners, `Loading theory` notices, `###` timing lines and the
    ML toplevel's `val it = (): unit` all land there, and so would an
    echoed `theorem name: prop` if `show_results=false` were ever
    dropped.
    """
    entries: list[TrustEntry] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = _TRUST_REPORT_LINE_RE.match(line)
        if not match:
            continue
        oracles_text = match.group("oracles").strip()
        oracles = (
            tuple(o.strip() for o in oracles_text.split(",") if o.strip())
            if oracles_text
            else ()
        )
        entries.append(
            TrustEntry(decl=match.group("thm"), assumptions=oracles, clean=not oracles)
        )
    return entries


# ---------------------------------------------------------------------------
# The profile
# ---------------------------------------------------------------------------

ISABELLE = ProverProfile(
    name="isabelle",
    file_extensions=(".thy",),
    comment_syntax=CommentSyntax(
        line=None,
        block_open="(*",
        block_close="*)",
        # The cartouche: inside it a `(*` is ordinary text, not a
        # comment opener. Read `CommentSyntax.verbatim_delimiters`
        # before touching this — omitting `\<open>(*)\<close>` blanks
        # real declarations across the distribution, and an invisible
        # declaration's statement is rewritable past a blocking check.
        # ASCII only, on measured evidence: the Unicode `‹…›` rendering
        # is rejected by `isabelle build`, does not appear in the
        # distribution, and treating `›` as a closer would end cartouche
        # state early on legal informal text.
        verbatim_delimiters=(_CARTOUCHE_OPEN, _CARTOUCHE_CLOSE),
    ),
    decl_keywords=_DECL_KEYWORDS,
    statement_keywords=_DECL_KEYWORDS,
    definition_keywords=_DEFINITION_KEYWORDS,
    definition_body_separator=_DEFINITION_BODY_SEPARATOR,
    # `sorry` and `oops` ONLY — `undefined` is deliberately absent.
    #
    # THE HOLE THIS LEAVES, stated here because this tuple is where the
    # next person will come to close it: an isabelle `definition f ::
    # nat where "f = undefined"` specifies nothing (`undefined` is HOL's
    # own unspecified constant, so the declaration names an arbitrary
    # inhabitant of its type) and **no gate check counts it** — not a
    # `sorry`, not an axiom or oracle, and it compiles. A worker who
    # writes it in a NEW declaration passes every check; stage-two
    # review is the only backstop, which `docs/agents/ORCHESTRATOR.md` says.
    #
    # DO NOT CLOSE IT BY ADDING `undefined` HERE. This tuple feeds
    # `gate.inventory.scan.scan_text` and so the **blocking**
    # `sorry-delta` check, so `undefined` here would make
    # `fun f :: "nat => nat" where "f 0 = 1" | "f _ = undefined"` — the
    # idiomatic spelling of a total function's don't-care branch — count
    # as a sorry and block legitimate work, and would put `undefined` in
    # the worker-facing "placeholders you may not add" list
    # `client.workspace` renders into CHOIR.md.
    #
    # AND `undefined` IS NOT A `sorry`. Measured on a live
    # Isabelle2025-2 against `definition unfilled :: nat where
    # "unfilled = undefined"`: `unfilled = unfilled`, `EX n. unfilled =
    # n` and `unfilled = 0 | unfilled ~= 0` all prove, while `unfilled =
    # 5` does NOT and neither does `False`. So an `undefined` body makes
    # nothing false provable — it makes the declaration EMPTY. Counting
    # it as a sorry would misreport that.
    #
    # IT IS ALSO NOT SYNTACTICALLY DISTINGUISHABLE. "Flag a declaration
    # whose every right-hand side is exactly `undefined`" was measured
    # against the corpus and disconfirmed: Isabelle/HOL has real content
    # in exactly that shape (`HOL/MicroJava/J/Type.thy`,
    # `JListExample.thy`, `JVM/JVMListExample.thy` — single-equation
    # definitions written that way deliberately for code generation).
    # Either way it is a rule over a declaration's BODY rather than a
    # token, so it cannot be wired into this tuple at all; it needs its
    # own advisory reporter plus right-hand-side slicing (the equation
    # separator differs by keyword, `=` for `definition`/`fun`/`primrec`
    # and `==` for `abbreviation`, and equations are `|`-separated).
    placeholder_tokens=("sorry", "oops"),
    trust_patterns=(
        ("axiomatization", r"^\s*axiomatization\b"),
        ("oracle", r"^\s*oracle\b"),
    ),
    build_command=("isabelle", "build", "-D", "."),
    toolchain_file=None,
    protected_files=("ROOT", "ROOTS"),
    extra_audits=(),
    reserved_non_names=(
        # Isar long-goal clause openers. Each is a reserved keyword, so
        # none can be a declaration name, and each can legally be the
        # first token after a bare `lemma`/`theorem` line — which is
        # exactly when the span scanner would otherwise capture it as one.
        "assumes",
        "fixes",
        "shows",
        "obtains",
        "notes",
        "defines",
        "constrains",
        "for",
        "if",
        "and",
        "where",
        "is",
    ),
    search_tooling_note=False,
    extract_statement=extract_isabelle_statement,
    trust_report_command=isabelle_trust_report_command,
    parse_trust_report=parse_isabelle_trust_report,
    decl_modifiers=_DECL_MODIFIERS,
    decl_target_syntax=_DECL_TARGET,
    decl_keyword_suffix=_DECL_KEYWORD_SUFFIX,
    decl_type_params_syntax=_DECL_TYPE_PARAMS,
    decl_continuation_lines=isabelle_continuation_lines,
    non_body_commands=_NON_BODY_COMMANDS,
    qualify_decl_names=qualify_isabelle_decl_names,
)


# The declaration-boundary regex, shared by `extract_isabelle_statement`
# (which terminates its header scan at the next declaration) and
# `qualify_isabelle_decl_names` (which needs the same notion of "this
# line starts a declaration" to key its scope paths). Both must also
# agree with `gate.verify.style.find_decl_spans`' span boundaries, or
# the audit extracts a statement from a region no span covers. Read off
# the profile rather than by a hand-written argument list, which is the
# drift `decl_line_regex_for` exists to prevent — hence its definition
# after `ISABELLE`; every consumer is a function body.
_DECL_LINE_RE = decl_line_regex_for(ISABELLE)
