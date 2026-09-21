"""Statement-equivalence audit (Phase 3, load-bearing).

The failure mode this catches: a PR that "proves" a `target_decl` by
silently *weakening* its statement — replacing `theorem foo : strong` +
`sorry` with `theorem foo : weak` + a proof of `weak`. The rebuild
passes (the file compiles) but the contract is broken.

v0 is the naive string-based check AGENTS.md anticipated: extract the
signature substring for `target_decl` in both base and head versions of
the file, normalize whitespace, compare. Extraction is delegated to the
effective `ProverProfile`'s `extract_statement` hook (design note 12
§2.2) — lean4's extractor (the keyword scan + balanced-bracket scan to
the top-level `:=`) now lives in `gate.provers.lean4`, moved there
byte-for-byte; this module's `extract_statement` is a thin wrapper kept
for existing callers, defaulting to the lean4 profile so behaviour is
unchanged. isabelle/rocq extractors are their own profiles'
syntax-appropriate rules (quoted-statement capture / sentence capture).
Every extractor is syntactic v0; planned iteration.

The audit produces three outcomes:

- **EQUIVALENT** — base and head signatures normalize to the same
  string. PR passes this check.
- **CHANGED** — signatures differ after normalization. PR fails.
- **UNDETERMINED** — either side couldn't be extracted (file missing,
  declaration not present in base, multi-line tricks the regex). The
  audit *passes* in this state — better to defer to other audits than
  to block PRs on parser limitations. Logged loudly for human review.
"""

from __future__ import annotations

from enum import Enum

from gate.provers.base import ProverProfile
from gate.provers.lean4 import LEAN4


class Verdict(Enum):
    EQUIVALENT = "equivalent"
    CHANGED = "changed"
    UNDETERMINED = "undetermined"


def extract_statement(
    file_contents: str, target_decl: str, *, profile: ProverProfile = LEAN4
) -> str | None:
    """Return the statement text for `target_decl` via the profile's extractor.

    Delegates to `profile.extract_statement` (lean4 by default, so
    existing callers are unaffected). See `gate.provers.lean4.extract_lean_statement`,
    `gate.provers.isabelle.extract_isabelle_statement`, and
    `gate.provers.rocq.extract_rocq_statement` for the per-prover rules.
    """
    return profile.extract_statement(file_contents, target_decl)


def normalize_statement(s: str) -> str:
    """Collapse runs of whitespace (any kind) to single spaces."""
    return " ".join(s.split())


def statements_equivalent(a: str, b: str) -> bool:
    """Whitespace-insensitive equality."""
    return normalize_statement(a) == normalize_statement(b)


def compare(
    base_contents: str,
    head_contents: str,
    target_decl: str,
    *,
    profile: ProverProfile = LEAN4,
) -> tuple[Verdict, str]:
    """Compare statements across base/head. Returns `(verdict, message)`.

    `profile` selects the statement extractor (defaults to lean4).
    """
    base = extract_statement(base_contents, target_decl, profile=profile)
    head = extract_statement(head_contents, target_decl, profile=profile)

    if base is None and head is None:
        return Verdict.UNDETERMINED, (
            f"could not locate `{target_decl}` in either base or head"
        )
    if base is None:
        return Verdict.UNDETERMINED, (
            f"could not locate `{target_decl}` in base (new declaration?)"
        )
    if head is None:
        return Verdict.UNDETERMINED, (
            f"could not locate `{target_decl}` in head"
        )

    if statements_equivalent(base, head):
        return Verdict.EQUIVALENT, (
            f"statement of `{target_decl}` is unchanged"
        )
    return Verdict.CHANGED, (
        f"statement of `{target_decl}` differs between base and head:\n\n"
        f"  base: {normalize_statement(base)}\n"
        f"  head: {normalize_statement(head)}"
    )
