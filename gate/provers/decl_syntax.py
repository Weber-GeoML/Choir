"""Shared prefix-aware declaration-line scanning.

`decl_line_regex` is the single source of truth for where a declaration
starts. It replaced four hand-rolled first-token-only copies (in
`gate/verify/style.py`, `gate/inventory/scan.py`,
`gate/indexer/extract.py`, `gate/provers/lean4.py`) that missed
`@[simp] theorem foo` — swallowed by the preceding declaration's span, a
false block — and `private theorem foo`, unenumerated, so retyping its
statement was invisible to every audit built on this enumeration.
`tests/gate/test_no_duplicate_decl_line_regex.py` guards a fifth copy.

**Do not invent prover syntax here.** An invented modifier is worse than
an omitted one: a non-declaration line read as a declaration start
invents a span boundary, shifting a neighbour's span and false-blocking
*it*. Every value wired into a `ProverProfile` was checked against the
prover's real grammar first, and each prover module carries the
provenance. Keywords and fragments arrive as plain parameters and this
module imports no prover module, so `gate/provers/lean4.py` (etc.) can
import it without a cycle.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from functools import cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only, see `decl_line_regex_for`
    from gate.provers.base import ProverProfile


def _fragment(field: str, value: str | None, template: str) -> str:
    r"""`template` filled with `value`; `""` when `value` is `None`.

    `value` is a **raw regex fragment supplied by the profile**, not a
    literal to escape: the token shape it matches is prover syntax and
    belongs in `gate/provers/<prover>.py` next to the grammar production
    it was transcribed from (see the module docstring). Two rules on such
    a fragment:

    - **No capturing groups**, refused here rather than trusted.
      `decl_line_regex` promises group 1 = keyword and group 2 = name, so
      a stray `(...)` silently renumbers them — worse for the fragments
      that sit *between* the two. `re.compile` also rejects a fragment
      that does not compile at all.
    - **Terminated argument only.** Not machine-checkable, so it is a
      contract on the caller: a fragment whose argument has no terminator
      (`Timeout\s+\S+`) is exactly the over-permissive prefix that
      invents span boundaries. Match the argument's real token shape (a
      numeral, a quoted string) so it cannot run past it.
    """
    if value is None:
        return ""
    if re.compile(value).groups:
        raise ValueError(
            f"{field} fragment {value!r} contains a capturing group; "
            "decl_line_regex's group 1 = keyword / group 2 = name "
            "contract requires (?:...) throughout"
        )
    return template.format(value)


def decl_prefix_fragment(
    modifiers: tuple[str, ...],
    attribute: tuple[str, str] | None,
    *,
    prefix_commands: tuple[str, ...] = (),
    prefix_flags: tuple[str, ...] = (),
) -> str:
    r"""Zero or more leading modifier / attribute / prefix-command groups.

    The four shapes that sit *before* the declaration keyword:

    - `modifiers` — matched literally (escaped).
    - `attribute` — an `(open, close)` pair (e.g. `("@[", "]")`) matched
      as `open + anything-but-close* + close`, which is a v0 scanner
      without bracket balancing, matching the rigor this module's callers
      already use.
    - `prefix_commands` — a command taking an argument and then scoping
      one declaration with `in` (lean4's `open Nat in theorem foo`,
      `set_option maxHeartbeats 400000 in theorem foo`). The keyword
      alone is not the whole prefix, so each entry matches as
      `keyword … in`, lazily up to the first whole-word `in`, so an
      argument that merely *starts* with "in" (`open interval in`) does
      not cut the match short.
    - `prefix_flags` — a prefix keyword taking a *terminated* argument
      and then the declaration, with no `in` and no bracket to close
      (rocq's `control_flag`s, `Timeout 10 Theorem t : P.`). Raw regex
      fragments under `_fragment`'s two rules.

    The result is ready to embed in front of a `(KEYWORDS)` group: it
    already carries the `*`-repetition and the trailing `\s+` each
    repetition consumes. Returns `""` when there is nothing to match, and
    introduces no capturing group, so `decl_line_regex`'s group numbering
    is unaffected by how many shapes a profile configures.
    """
    alternatives = [re.escape(modifier) for modifier in modifiers]
    if attribute is not None:
        open_token, close_token = attribute
        alternatives.append(
            rf"{re.escape(open_token)}[^{re.escape(close_token)}]*{re.escape(close_token)}"
        )
    alternatives.extend(
        rf"{re.escape(command)}\b[^\n]*?\bin\b" for command in prefix_commands
    )
    alternatives.extend(_fragment("prefix_flags", flag, "{}") for flag in prefix_flags)
    if not alternatives:
        return ""
    group = "|".join(alternatives)
    return rf"(?:(?:{group})\s+)*"


def decl_target_fragment(target: str | None) -> str:
    r"""An optional target group between the keyword and the name.

    isabelle's `lemma (in A) foo:`. `Pure/Isar/parse.ML`'s `opt_target`
    is spliced in by `Outer_Syntax.local_theory*`, so *every*
    `local_theory` command accepts it — which is most of the commands
    that introduce a declaration — and without this the name group
    captured the literal `(in`.

    Emitted as `(?:TARGET\s+)?`. **Optional is load-bearing in two
    directions**: a declaration written without a target still matches,
    and one whose target is the last thing on its line (`lemma (in A)`
    with the name on the following line — legal Isabelle, verified)
    still matches with the old raw-token name `(in`, because the engine
    backtracks out of the optional group. Failing to match instead would
    delete the declaration from enumeration entirely — the fail-open this
    module exists to close.
    """
    return _fragment("decl_target_syntax", target, r"(?:{}\s+)?")


def decl_keyword_suffix_fragment(suffix: str | None) -> str:
    r"""Decorations attached TO the keyword, with no whitespace between.

    Neither `decl_prefix_fragment` (before the keyword, consuming a `\s+`
    per repetition) nor `decl_target_fragment` (after the keyword's
    `\s+`) can express isabelle's three such spellings, all of which made
    `decl_line_regex` fail to match at all, leaving the declaration
    unenumerated and its statement freely rewritable:

        definition\<^marker>\<open>tag important\<close> foo :: nat where …
        lift_definition(code_dt) foo is …
        theorem%important foo: "…"

    Emitted as `(?:SUFFIX)*` — repeatable, because the spellings stack
    (`definition%important\<^marker>\<open>…\<close>` is legal).
    """
    return _fragment("decl_keyword_suffix", suffix, r"(?:{})*")


def decl_type_params_fragment(type_params: str | None) -> str:
    r"""Type parameters written BEFORE the name.

    isabelle writes `datatype ('a, 'b) t`, `datatype 'a tree`,
    `typedef (overloaded) ('a, 'b :: len) vec`, so the name group
    captured `('a,` / `'a` / `(overloaded)`. Not a fail-open — the
    declaration IS enumerated — but keyed on something that is not an
    identifier, so every same-keyword type declaration in one theory
    collided.

    Emitted as `(?:TYPE_PARAMS\s+)*`, repeatable so the
    `typedef (overloaded) ('a, 'b) vec` two-group spelling resolves.
    """
    return _fragment("decl_type_params", type_params, r"(?:{}\s+)*")


def decl_line_regex(
    decl_keywords: tuple[str, ...],
    *,
    modifiers: tuple[str, ...] = (),
    attribute: tuple[str, str] | None = None,
    prefix_commands: tuple[str, ...] = (),
    prefix_flags: tuple[str, ...] = (),
    target: str | None = None,
    keyword_suffix: str | None = None,
    type_params: str | None = None,
) -> re.Pattern[str]:
    r"""Compile `PREFIX* (KEYWORDS) SUFFIX* (\s+ TARGET? TP* (NAME) | \s*$)`.

    Group 1 is the matched decl keyword; group 2 is the raw name token
    (everything up to the next whitespace — `normalize_decl_name` turns
    that into a key). Every keyword argument defaults to empty/`None`, so
    a caller that passes none gets the plain unprefixed pattern. Prefer
    `decl_line_regex_for` over passing them by hand; see
    `decl_prefix_fragment`, `decl_target_fragment`,
    `decl_keyword_suffix_fragment` and `decl_type_params_fragment` for
    what each shape covers.

    **Group 2 is `None` when the keyword ends its line**: `definition` on
    one line and `  gfp :: "…" where …` on the next is ordinary Isabelle,
    not an edge case, and a pattern requiring `\s+(\S+)` matched none of
    them, so those declarations were absent from every enumeration built
    on this regex.
    The name cannot be read from the line, so this pattern reports the
    *boundary* and `decl_name_from` resolves the name from the following
    lines; every consumer goes through that helper rather than
    `match.group(2)`. Written prover-blind because the shape is legal in
    all three grammars (inert on lean4, where no toolchain line uses it).

    Modifiers/attribute groups may repeat and stack in any order (real
    declarations do: `@[simp] private theorem foo`), which is
    deliberately looser than any one prover's grammar: an
    over-permissive prefix on a line that does not occur in real code
    changes nothing, whereas under-recognising a real prefixed
    declaration is the fail-open this module exists to fix.
    """
    keyword_alt = "|".join(re.escape(keyword) for keyword in decl_keywords)
    prefix = decl_prefix_fragment(
        modifiers,
        attribute,
        prefix_commands=prefix_commands,
        prefix_flags=prefix_flags,
    )
    suffix_frag = decl_keyword_suffix_fragment(keyword_suffix)
    target_frag = decl_target_fragment(target)
    type_params_frag = decl_type_params_fragment(type_params)
    return re.compile(
        rf"^\s*{prefix}({keyword_alt}){suffix_frag}"
        rf"(?:\s+{target_frag}{type_params_frag}(\S+)|\s*$)"
    )


def decl_name_from(match: re.Match[str], lines: Sequence[str], index: int) -> str:
    r"""The declaration's enumeration key for one `decl_line_regex` match.

    `normalize_decl_name(match.group(2))` in the ordinary case. This
    helper exists because group 2 is `None` when the keyword ends its
    line, and a line-local regex cannot reach the next line for the name.
    **Every consumer must go through it**: one left behind does not fail
    loudly, and here it would be worse than silent —
    `normalize_decl_name(None)` raises.

    `lines` must be the **comment-blanked** copy every consumer already
    matches against, aligned 1:1 with the real file, so a `theorem foo`
    written inside a comment cannot supply a name. `index` is the 0-based
    index of the matched line.

    Resolution scans forward to the first non-blank line and takes its
    first whitespace-delimited token — a real name for the definitional
    spellings (`definition` / `  gfp :: …` gives `gfp`) and a
    junk-but-stable key otherwise (`lemma` / `  [code]: "…"` gives
    `[code]:`). That asymmetry was measured before being accepted: junk
    keys move those declarations from *unenumerated*, where a statement
    rewrite is invisible, to *duplicate-name UNDETERMINED*, which blocks.
    Falls back to the keyword at EOF, so the key is never empty.
    """
    raw = match.group(2)
    if raw is not None:
        return normalize_decl_name(raw)
    for line in lines[index + 1 :]:
        stripped = line.strip()
        if stripped:
            return normalize_decl_name(stripped.split()[0])
    return normalize_decl_name(match.group(1))


@cache
def decl_line_regex_for(
    profile: ProverProfile, *, keywords: tuple[str, ...] | None = None
) -> re.Pattern[str]:
    """`decl_line_regex` with every prefix shape read off one profile.

    The argument list lives here instead of at each `gate/` consumer
    because every time this module grows a prefix shape, a call site that
    is missed does not fail loudly — it silently keeps the fail-open the
    new shape was added to close, at that one consumer.

    `keywords` overrides `profile.decl_keywords` for a caller that scans
    a narrower kind set; the prefix shapes always come from the profile.

    Cached: profiles are frozen and module-level, so repeated calls
    return one compiled pattern. Typed against `ProverProfile` under
    `TYPE_CHECKING` only, so this module keeps the *runtime* independence
    its docstring relies on.
    """
    return decl_line_regex(
        profile.decl_keywords if keywords is None else keywords,
        modifiers=profile.decl_modifiers,
        attribute=profile.attribute_syntax,
        prefix_commands=profile.decl_prefix_commands,
        prefix_flags=profile.decl_prefix_flags,
        target=profile.decl_target_syntax,
        keyword_suffix=profile.decl_keyword_suffix,
        type_params=profile.decl_type_params_syntax,
    )


def continuation_lines_for(
    profile: ProverProfile, lines: Sequence[str]
) -> frozenset[int]:
    r"""Lines that are NOT declaration starts, per one profile.

    Every enumeration built on `decl_line_regex` matches line by line
    with no memory, and on isabelle that mis-splits a legitimate
    multi-line quoted header — this BUILDS on the live toolchain:

        definition ff :: "(nat, nat)
          fun " where "ff = (\<lambda>n. n)"

    `fun` there is HOL's postfix function-type constructor inside an
    *open* quoted type, not a command, so the second line matched as a
    declaration (keyword `fun`, name `"`) — truncating `ff`'s span, which
    left the rest of `ff` uncompared, and inventing a phantom beside it.

    The rule itself is `ProverProfile.decl_continuation_lines`, a
    prover-supplied callable because it needs the prover's own lexer
    (isabelle must track `"…"` and `\<open>…\<close>` jointly) and this
    module must not invent prover syntax. This wrapper only spells the
    "profile has no rule" case once instead of at each consumer; it
    returns an empty set for lean4 and rocq.
    """
    hook = profile.decl_continuation_lines
    return frozenset() if hook is None else hook(lines)


def normalize_decl_name(raw: str) -> str:
    r"""Strip separator punctuation the name capture group swallows.

    The name group is `(\S+)` — everything up to the next whitespace — so
    a declaration written with no space before its trailing punctuation
    captures it as part of the name. That is the dominant Isar style
    (`lemma foo: "P x"`), not an edge case: unnormalized, the bad name
    flows into `profile.extract_statement`, whose `prefix_re` ends in
    `\b` (isabelle/rocq), and there is no word boundary between `:` and
    the following space — so the match fails and statement-immutability
    reports `UNDETERMINED`, which fails by design. Truncating at the
    first `[` additionally drops an Isabelle attribute list
    (`foo[simp]:`).

    Do NOT instead exclude `:`/`,`/`[` from the regex's character class —
    that was tried and rejected. Anonymous declarations (`example : T :=
    …`, a bare `instance : Foo X where …`) have no name at all, and
    `(\S+)` capturing the literal `:` for them is what lets those forms
    resolve today; excluding punctuation would make the regex not match
    those lines, so the declaration would vanish from enumeration and
    stop being compared — a false block turned into a worse fail-open.

    The output is therefore an *enumeration* key, not necessarily a name
    any prover can resolve — `:` for an anonymous declaration, `(n`/`{α`
    for a binder-first header, `'a` for `datatype 'a tree`. Consumers
    that compare keys against keys are fine with that; one that hands the
    name to the prover must filter, as
    `gate.verify.changed_decls._is_probeable_name` does.
    """
    name = raw.split("[", 1)[0].rstrip(":,")
    if not name:
        # Degenerate/anonymous forms (a bare ":", or "[simp]:" with
        # nothing before the bracket) — keep the raw token rather than
        # emptying it, so anonymous declarations still get a non-empty key.
        name = raw
    return name


def qualify_by_scope(
    text: str,
    *,
    decl_re: re.Pattern[str],
    qualifying_open_re: re.Pattern[str],
    close_re: re.Pattern[str],
    plain_open_re: re.Pattern[str] | None = None,
    commit_re: re.Pattern[str] | None = None,
    decl_target_re: re.Pattern[str] | None = None,
    continuation_lines: Callable[[Sequence[str]], frozenset[int]] | None = None,
) -> dict[int, str]:
    """Map each declaration's line number to an enclosing-scope path.

    **This is a disambiguation key, NOT name resolution.** Do not build
    anything on it that needs the name a prover would actually resolve.
    It answers exactly one question, for
    `gate.verify.statement_immutability`: *which of the several
    same-short-named declarations in this one file is which?* A key
    serving that purpose has to be stable, derived from the file's own
    structure, and computed identically on the base and head sides — and
    nothing else.

    That distinction is why this function can exist at all. A *correct*
    qualifier would need nested modules, functor application,
    `Module Type`, `Import`/`Export`/`Include`, locale interpretation and
    `sublocale` — none of it verifiable without those toolchains. An
    enclosing-scope path is wrong-but-consistent in precisely those
    cases, and consistency is all the grouping needs: both sides run this
    same function over their own text, so a semantically wrong path still
    matches base against head, and the only cost is that two
    declarations which could have been told apart collide instead —
    which is the `UNDETERMINED` these provers already reported for every
    such pair. It degrades to the previous behaviour; it cannot do worse.

    The same property bounds an unbalanced stack. Any prover has
    scope-like constructs this function does not know (isabelle's
    `instantiation`/`bundle`/`notepad`, a rocq functor `Module F (X : S).`),
    and one of those closers can pop a scope it did not open, mis-keying
    every declaration after it — harmlessly, because it happens
    identically on both sides. It becomes a false `CHANGED` only if a
    worker's own edit adds or removes a scope opener/closer, which a
    proof-body fill does not do.

    Parameters, all regexes matched against a whole line:

    - `decl_re` — the prover's declaration-boundary regex, group 2 the
      raw name token (`decl_line_regex`'s contract). Names go through
      `decl_name_from`, so these keys agree with the span names
      `gate.verify.style.find_decl_spans` produces from the same regex.
    - `qualifying_open_re` — opens a scope that CONTRIBUTES its group-1
      name to the path (rocq `Module`, isabelle `locale`/`context X`). An
      opener line that ALSO matches `decl_re` gets a key of its own,
      under the enclosing path rather than its own: isabelle's
      `locale`/`class` are both scopes and declarations, because their
      `assumes` clauses are a trust surface that has to be compared.
    - `plain_open_re` — opens a scope that contributes nothing to the
      path but must still be tracked, so its closer does not pop an
      enclosing contributing scope (rocq's `Section`, an anonymous
      isabelle `context begin`). **Known limitation:** its own *name* is
      not tracked either, so a named closer pops the innermost entry
      rather than the matching named one. Acceptable where the result is
      only a disambiguation key (isabelle, rocq); lean4 deliberately
      does not use this walk at all, because
      `gate.provers.lean4.qualify_decl_names` has to keep a section's
      name — `section A` inside `namespace A` is valid Lean whose `end A`
      closes the section, and dropping the name pops the namespace
      instead, so the declaration after it loses its `A.` prefix.
    - `close_re` — closes the innermost scope; group 1, if the pattern
      has one and it matched, names which scope to close (rocq's
      `End A.`), tolerating out-of-order closes the way lean4's
      `_pop_innermost` does. isabelle's bare `end` has no name.
    - `commit_re` — for a prover where opening is two steps: isabelle
      writes `locale A = B + C` possibly across several lines and the
      scope only opens at the following `begin`, so the opener *arms* a
      pending name and this pattern commits it. `None` (rocq) means an
      opener pushes immediately.
    - `decl_target_re` — for a prover whose declaration commands can name
      their scope *inline* instead of being written inside its block:
      isabelle's `lemma (in A) foo` is the same declaration, under the
      same key, as a `lemma foo` inside `locale A … begin`. When it
      matches, its group 1 **replaces** the enclosing-scope path rather
      than extending it, because an immediate target *suspends* the
      current target context for that one command (Isar reference manual
      §5.2), so `lemma (in B) foo` inside `locale A` is `B.foo`, not
      `A.B.foo`. A match whose group 1 did not participate (`None`) means
      the global scope — isabelle spells that `(in -)` — and yields the
      bare name. No match at all leaves the stack path untouched, so an
      unrecognised target spelling degrades to the coarser key rather
      than a wrong one.
    - `continuation_lines` — `ProverProfile.decl_continuation_lines`, so
      this enumeration skips exactly the lines
      `gate.verify.style.find_decl_spans` skips. Keeping the two in step
      is not cosmetic:
      `gate.verify.statement_immutability._decl_key` looks a span's start
      line up in this map. Pass the same comment-blanked text the span
      scanner uses.

    Returns `{1-indexed line: "A.B.name"}`, with the bare name for a
    declaration in no contributing scope — the shape
    `ProverProfile.qualify_decl_names` expects.
    """
    lines = text.splitlines()
    continuation = (
        frozenset() if continuation_lines is None else continuation_lines(lines)
    )
    # (contributes, name) — `name` is None for an anonymous scope.
    stack: list[tuple[bool, str | None]] = []
    pending: tuple[bool, str | None] | None = None
    result: dict[int, str] = {}

    for line_no, line in enumerate(lines, start=1):
        if line_no - 1 in continuation:
            continue
        close_match = close_re.match(line)
        if close_match is not None:
            _pop_scope(stack, _match_group_or_none(close_match))
            # A closer cannot also be an opener or a declaration.
            pending = None
            continue

        opened = False
        open_match = qualifying_open_re.match(line)
        if open_match is not None:
            entry = (True, _match_group_or_none(open_match))
            opened = True
        elif plain_open_re is not None:
            plain_match = plain_open_re.match(line)
            if plain_match is not None:
                entry = (False, None)
                opened = True

        if opened:
            # A scope opener may ALSO be an enumerated declaration —
            # isabelle's `locale`/`class` headers carry `assumes` clauses
            # every theorem in the scope depends on. Its own key uses the
            # ENCLOSING path, computed before the push, so `locale B`
            # inside `locale A` keys as `A.B` rather than `A.B.B`. Inert
            # for a prover whose scope openers are not declaration
            # keywords: rocq's `Module`/`Section` are deliberately absent
            # from `gate.provers.rocq`'s `_DECL_KEYWORDS`.
            opener_decl = decl_re.match(line)
            if opener_decl is not None:
                result[line_no] = _scope_path(
                    stack,
                    opener_decl,
                    lines,
                    line_no - 1,
                    target_match=_target(decl_target_re, line),
                )
            if commit_re is None:
                stack.append(entry)
                pending = None
            else:
                pending = entry
            # Fall through: isabelle writes `locale A begin` on one
            # line, so the same line that arms the scope can commit it.

        if (
            commit_re is not None
            and pending is not None
            and commit_re.search(line) is not None
        ):
            stack.append(pending)
            pending = None
        if opened:
            continue

        decl_match = decl_re.match(line)
        if decl_match is not None:
            result[line_no] = _scope_path(
                stack,
                decl_match,
                lines,
                line_no - 1,
                target_match=_target(decl_target_re, line),
            )
    return result


def _target(
    decl_target_re: re.Pattern[str] | None, line: str
) -> re.Match[str] | None:
    """`decl_target_re` against `line`, or `None` when unconfigured.

    One helper so both call sites in `qualify_by_scope` read the inline
    target the same way.
    """
    if decl_target_re is None:
        return None
    return decl_target_re.match(line)


def _scope_path(
    stack: list[tuple[bool, str | None]],
    decl_match: re.Match[str],
    lines: Sequence[str],
    index: int,
    *,
    target_match: re.Match[str] | None = None,
) -> str:
    """`A.B.name` for a declaration matched inside `stack`.

    Only contributing, named scopes prefix the path. `target_match` is
    `qualify_by_scope`'s `decl_target_re` match, when the declaration
    names its scope inline; it REPLACES the stack path rather than
    extending it (see that function's parameter note for the Isar rule),
    and a match whose group 1 did not participate is the prover's
    global-scope spelling, i.e. no path at all.
    """
    if target_match is not None:
        root = target_match.group(1)
        prefix = [root] if root else []
    else:
        prefix = [name for contributes, name in stack if contributes and name]
    surface = decl_name_from(decl_match, lines, index)
    return ".".join([*prefix, surface]) if prefix else surface


def _match_group_or_none(match: re.Match[str]) -> str | None:
    """Group 1 of `match`, or `None` when the pattern has no group 1.

    Scope openers/closers differ in whether they name the scope (`End A.`
    does, isabelle's bare `end` does not), and `.group(1)` raises on a
    pattern written without one — so ask the match how many it has.
    """
    if match.re.groups < 1:
        return None
    return match.group(1)


def _pop_scope(stack: list[tuple[bool, str | None]], name: str | None) -> None:
    """Pop `stack` for a closer, by name when the closer names one.

    Deliberately the same rule as `gate.provers.lean4._pop_innermost`
    (which stays where it is — lean4 keeps its own validated qualifier):
    an unnamed closer pops the innermost entry; a named one removes the
    topmost matching entry, tolerating out-of-order closes in malformed
    input, and falls back to the innermost so the stack still converges.
    Popping an empty stack is a no-op — every prover here has closers
    this function does not pair with an opener it recognises (an isabelle
    theory's own `begin`/`end` wrapper, most obviously), and swallowing
    those is what keeps the key stable on ordinary input.
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
