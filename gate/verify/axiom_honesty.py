"""Axiom-honesty audit (Phase 3).

Flags PRs that introduce new uses of axiomatic or trust-eroding
constructs without explicit human review. AGENTS.md non-negotiable:

> Axiom-dependency audit (any new `axiom`, `unsafe`, `native_decide`,
> `partial`, `extern` blocks merge until human-reviewed)

v0 is a count-and-compare across all files touched by the PR: for each
pattern, count occurrences in the base version and the head version
of the file. If head > base for any pattern, the audit flags the PR.

Generalized to any `gate.provers.ProverProfile` (design note 12 §3):
`compare`'s `profile` argument selects which trust patterns are
watched — lean4's five patterns by default, or a project's configured
prover (isabelle's `axiomatization`/`oracle`, rocq's
`axiom`/`admitted`/`native_compute`/`vm_compute`/`universe_checking`/
`ml_module`). Every profile's patterns support the `net_zero` policy
without further work.

What this catches and doesn't:
- Catches direct introductions (`axiom foo : T` lands where there was
  none before; a `partial def` is added; `native_decide` newly appears)
  for whichever prover's patterns are in effect.
- Doesn't catch hypothesis-as-axiom (a theorem with `(h : P)` where
  the maintainer never intends to provide a witness for P — the
  axiom is hidden in the hypothesis). AGENTS.md flags this as a
  Phase 3+ concern; needs real per-prover parsing.
- Doesn't catch reformulations that move axiomatic content elsewhere
  (e.g., declaring an axiom in a separate file, importing). v0 is
  per-file.
- False positives possible from comments mentioning these keywords —
  acceptable trade-off in v0; real iteration uses lex-aware scanning.
- Whitelist policy (name-matching individual `axiom NAME` declarations
  against `allowed_axioms`) only works where the profile has a real
  axiom-name extractor — lean4 only today (`extract_axiom_names`,
  `_supports_whitelist`). Requesting whitelist policy on a profile
  without one (e.g. rocq) fails closed: `compare` degrades to net-zero
  for that profile's `"axiom"`-labeled pattern rather than silently
  passing it, and the finding/report carries an explicit note that
  whitelist name-matching was unsupported and net-zero was enforced
  instead. Isabelle's assumption-introduction pattern is labeled
  `"axiomatization"`, not `"axiom"`, so it never reaches the whitelist
  branch at all — it always uses net-zero.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from gate.provers.base import ProverProfile
from gate.provers.lean4 import LEAN4
from gate.verify.config import AxiomHonestyConfig, AxiomPolicy


class Verdict(Enum):
    CLEAN = "clean"
    INTRODUCED = "introduced"


@dataclass(frozen=True)
class Finding:
    pattern: str
    base_count: int
    head_count: int
    # Set when the finding is an axiom-whitelist violation rather than
    # a net-zero increase; carries the disallowed axiom names so the
    # report can name them.
    disallowed_names: tuple[str, ...] = ()
    # Set when whitelist policy was requested but this profile has no
    # axiom-name extractor (see `_supports_whitelist`) — the finding is
    # really a net-zero comparison, and this note explains why.
    note: str | None = None

    @property
    def delta(self) -> int:
        return self.head_count - self.base_count


# Captures the axiom NAME (whatever identifier follows the `axiom`
# keyword). Used by the whitelist policy to check names against the
# project's allowed set. The character class accepts Lean identifier
# characters including dot and apostrophe. Lean-syntax-specific by
# construction — whitelist policy (unlike net_zero) is not generalized
# to isabelle/rocq's differently-shaped axiom syntax; see module note
# on `compare`'s `profile` parameter below.
_AXIOM_NAME_RE = re.compile(r"\baxiom\s+([A-Za-z_][A-Za-z0-9_.']*)")


def compile_patterns(
    patterns: tuple[tuple[str, str], ...],
) -> dict[str, re.Pattern[str]]:
    """Compile a profile's `trust_patterns` (label, regex-source) pairs.

    Always compiled with `re.MULTILINE`: isabelle's and rocq's patterns
    use `^\\s*` anchors to match top-level commands; lean4's patterns
    don't rely on `^` at all, so the flag is harmless there. Compiling
    uniformly means one code path for every profile.
    """
    return {name: re.compile(source, re.MULTILINE) for name, source in patterns}


# `\b` works for these because all keywords are composed of [a-z_]
# (word chars) and we want word-boundary semantics. `axiom` requires a
# following identifier (so `axiom foo : T`); the others can appear
# standalone or as modifiers. Mirrors `gate.provers.lean4.LEAN4.trust_patterns`
# verbatim (design note 12 §3.1) — kept as the module-level default so
# existing callers/tests are unaffected by the profile parameterization
# below.
_PATTERNS: dict[str, re.Pattern[str]] = compile_patterns(LEAN4.trust_patterns)


def count_patterns(
    file_contents: str, *, patterns: dict[str, re.Pattern[str]] | None = None
) -> dict[str, int]:
    """Count occurrences of each watched pattern in the file.

    `patterns` defaults to lean4's five patterns (`_PATTERNS`) so
    existing callers are unaffected; pass a profile's compiled
    `trust_patterns` (via `compile_patterns`) to count a different
    prover's trust-eroding constructs.
    """
    active = patterns if patterns is not None else _PATTERNS
    return {name: len(p.findall(file_contents)) for name, p in active.items()}


def extract_axiom_names(file_contents: str) -> list[str]:
    """Return the names of every `axiom NAME` declaration in the file.

    Names are returned in occurrence order; duplicates preserved. Lean4
    syntax only — see `_AXIOM_NAME_RE`'s note.
    """
    return _AXIOM_NAME_RE.findall(file_contents)


# Profiles whose "axiom"-labeled trust pattern can be name-matched against
# a whitelist. `extract_axiom_names` is lean4-syntax-specific (its regex
# assumes the `axiom NAME` keyword shape); rocq's `Axiom NAME : T.` (and
# `Parameter`/`Conjecture`) would need its own extractor to support
# whitelist mode honestly, and isabelle's pattern is labeled
# `"axiomatization"` so it never reaches this branch at all. This is a
# local capability map rather than a `ProverProfile` field — it's a
# single-consumer (whitelist policy) concern for now; promote it to a
# real per-profile extractor hook (design note 12 §9) if a second
# profile needs whitelist support.
_WHITELIST_CAPABLE_PROFILES = frozenset({"lean4"})


def _supports_whitelist(profile: ProverProfile) -> bool:
    """Whether `profile` has a real axiom-name extractor for whitelist mode.

    When this is `False`, `compare` fails closed: whitelist policy
    degrades to net-zero for the "axiom"-labeled pattern instead of
    silently passing (see `compare`'s docstring).
    """
    return profile.name in _WHITELIST_CAPABLE_PROFILES


def compare(
    base_contents: str,
    head_contents: str,
    *,
    config: AxiomHonestyConfig | None = None,
    profile: ProverProfile = LEAN4,
) -> tuple[Verdict, list[Finding]]:
    """Compare base/head per the configured audit policy.

    `profile` selects which trust patterns are watched (design note 12
    §3); defaults to lean4 so existing callers are unaffected. Every
    prover's patterns support the `net_zero` policy. The `whitelist`
    policy additionally names disallowed *axiom* declarations — it only
    activates when the profile has a pattern labeled `"axiom"` (lean4
    and rocq both do; isabelle's assumption-introduction pattern is
    labeled `"axiomatization"` instead) *and* the profile supports name
    extraction (`_supports_whitelist` — lean4 only today, since
    `extract_axiom_names` is lean4-syntax-specific).

    Fails closed for profiles that have an `"axiom"` pattern but no name
    extractor (currently rocq): whitelist policy degrades to net-zero
    comparison for that pattern instead of silently passing it through —
    the alternative (skipping the pattern in both branches) would let
    e.g. a bare `Axiom bad : False.` merge as clean under whitelist on a
    rocq project, since name-matching can't run and net-zero was being
    skipped for it too. The degraded finding carries a `note` explaining
    that whitelist name-matching is unsupported for the profile so the
    report doesn't imply real whitelist enforcement happened.

    Default policy (`net_zero`): any watched pattern whose count
    increases flags the PR.

    Findings are sorted by pattern name for deterministic output.
    """
    cfg = config or AxiomHonestyConfig()
    patterns = compile_patterns(profile.trust_patterns)
    base_counts = count_patterns(base_contents, patterns=patterns)
    head_counts = count_patterns(head_contents, patterns=patterns)

    findings: list[Finding] = []

    whitelist_for_axiom = cfg.policy is AxiomPolicy.WHITELIST and "axiom" in patterns

    if whitelist_for_axiom and _supports_whitelist(profile):
        # Whitelist for `axiom` specifically: any name in head not on
        # the allowed list is a violation, even if it was in base.
        head_names = extract_axiom_names(head_contents)
        allowed = set(cfg.allowed_axioms)
        disallowed = tuple(n for n in head_names if n not in allowed)
        if disallowed:
            # Deduplicate while preserving first-occurrence order.
            seen: set[str] = set()
            unique = tuple(n for n in disallowed if not (n in seen or seen.add(n)))
            findings.append(
                Finding(
                    pattern="axiom",
                    base_count=base_counts["axiom"],
                    head_count=head_counts["axiom"],
                    disallowed_names=unique,
                )
            )
        # The other patterns use net-zero even under whitelist policy.
        for name in sorted(patterns):
            if name == "axiom":
                continue
            if head_counts[name] > base_counts[name]:
                findings.append(
                    Finding(
                        pattern=name,
                        base_count=base_counts[name],
                        head_count=head_counts[name],
                    )
                )
    else:
        # Net-zero policy; whitelist with no "axiom" pattern in this
        # profile; or whitelist requested on a profile without an axiom
        # name extractor — all three fall through to plain count
        # comparison for every watched pattern.
        unsupported_note = (
            f"Whitelist axiom-name matching is not supported for prover "
            f"'{profile.name}' (no axiom-name extractor); net-zero was "
            f"enforced for the axiom-labeled pattern instead."
            if whitelist_for_axiom
            else None
        )
        findings = [
            Finding(
                pattern=name,
                base_count=base_counts[name],
                head_count=head_counts[name],
                note=unsupported_note if name == "axiom" else None,
            )
            for name in sorted(patterns)
            if head_counts[name] > base_counts[name]
        ]

    if findings:
        return Verdict.INTRODUCED, findings
    return Verdict.CLEAN, []


def format_findings(findings: list[Finding]) -> str:
    """Render findings as a Markdown table for status-check output."""
    if not findings:
        return "No new axiomatic constructs."
    rows = "\n".join(
        f"| `{f.pattern}` | {f.base_count} | {f.head_count} | **+{f.delta}** |"
        for f in findings
    )
    output = (
        "| pattern | base | head | delta |\n"
        "|---|---|---|---|\n"
        f"{rows}"
    )
    # If any finding was a whitelist violation, also name the disallowed
    # axioms — the maintainer needs to see which names to either add to
    # the whitelist or reject.
    for f in findings:
        if f.disallowed_names:
            names = ", ".join(f"`{n}`" for n in f.disallowed_names)
            output += (
                f"\n\nAxiom names not in the project whitelist: {names}"
            )
        if f.note:
            output += f"\n\n{f.note}"
    return output
