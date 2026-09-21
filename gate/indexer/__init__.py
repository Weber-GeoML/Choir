"""Coherence indexer (Phase 5, v0 — local-project scope only).

The indexer maintains a searchable map of declaration names across a
Choir-managed project and surfaces potential duplicates on every PR.
Per design note 04, the indexer is search-based, not LLM-per-PR —
avoiding the waste of agent invocations for routine
maintenance work.

v0 scope (this implementation):

- Scan all `.lean` files in the project at the PR head.
- For each top-level declaration, record (file, line, name).
- For each declaration newly introduced in the PR, check whether its
  unqualified last-segment name appears elsewhere in the project.
- Emit findings as a PR comment. Informational; doesn't block merge.

What's NOT in v0 (deferred):

- Mathlib-wide duplicate detection. Would require downloading a
  Mathlib index periodically — feasible but adds infrastructure.
- Namespace-aware fully-qualified name resolution. v0 uses the
  last-segment name only, which over-collects (`Foo.bar` and
  `Baz.bar` both register as "bar"). Future iteration uses Lean
  parsing to compute true qualified names.
- Signature-equivalence detection. Two declarations with different
  names but the same type are arguably duplicates — needs
  elaboration. Defer to a Lean-REPL-backed indexer.
- Persistent / scheduled refresh. v0 regenerates the scan on each
  PR; for very large projects (~10k+ declarations) we'd want a
  scheduled refresh writing to a cached index file.
"""
