"""Trust-boundary inventory: every axiom and sorry in a Lean tree.

The orchestrator agent's replanning input. "What is currently assumed
(axioms) and what is still unproven (sorries)?" is the question this
module answers — per file, per line, with the enclosing declaration —
so the orchestrator knows the project's trust boundary and what tasks
to publish next.

Read-only and pure: scans a local checkout, touches no GitHub state.

CLI::

    python -m gate.inventory scan <path> [--format json|md]

What v0 catches and doesn't:
- Catches `axiom NAME ...` declarations and `sorry` terms/tactics in
  source, with line/block comments stripped first (so prose mentioning
  "sorry" doesn't pollute the inventory).
- Doesn't catch `sorryAx` invoked directly, axioms introduced via
  metaprogramming, or other elaborator-level tricks. Those need the
  kernel's own accounting (`lean --print-axioms`) — the planned
  `verify-axiom-trace` workflow (design note 05) is the v1 answer.
  The deterministic gate audits remain the merge-time defense; this
  inventory is a planning instrument, not a security boundary.
"""

from gate.inventory.scan import (
    AxiomItem,
    SorryItem,
    TrustBoundary,
    declaration_names,
    scan_text,
    scan_tree,
    strip_comments,
)

__all__ = [
    "AxiomItem",
    "SorryItem",
    "TrustBoundary",
    "declaration_names",
    "scan_text",
    "scan_tree",
    "strip_comments",
]
