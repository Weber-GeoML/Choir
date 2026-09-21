"""Prover-profile registry (design note 12 §2).

Profiles are Choir-shipped code, never project config: a project
*selects* a profile via `.choir/project.toml`'s `[project] prover` key
(read gate-side by `gate/provers/select.py`), but a project can never
*define* one. Letting a repo define its own trust tokens, placeholder
tokens, or extractors would let a PR weaken its own gate — the same
non-configurability principle as the deterministic audits themselves
(design note 12 §2.1).

Absent `[project] prover` selects `"lean4"` everywhere — full
back-compat with the pre-generalization behavior.
"""

from __future__ import annotations

from gate.provers.base import CommentSyntax, ProverProfile, TrustEntry
from gate.provers.isabelle import ISABELLE
from gate.provers.lean4 import LEAN4
from gate.provers.rocq import ROCQ


class ProverError(RuntimeError):
    """An unknown prover name, or a malformed prover-selection config."""


PROFILES: dict[str, ProverProfile] = {
    "lean4": LEAN4,
    "isabelle": ISABELLE,
    "rocq": ROCQ,
}


def get_profile(name: str) -> ProverProfile:
    """Look up a profile by registry key.

    Raises `ProverError` (never a bare `KeyError`) naming the valid
    set, so an unknown prover name fails with an actionable message.
    """
    try:
        return PROFILES[name]
    except KeyError:
        valid = ", ".join(sorted(PROFILES))
        raise ProverError(f"unknown prover {name!r}; valid: {valid}") from None


__all__ = [
    "PROFILES",
    "CommentSyntax",
    "ProverError",
    "ProverProfile",
    "TrustEntry",
    "get_profile",
]
