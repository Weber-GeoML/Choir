"""Choir gate — the deterministic trust substrate.

Modules in this package are called from `.github/workflows/` YAMLs:
task intake validation, the verify audits, lifecycle transitions, and
claim reconciliation. The gate is deterministic by design — it admits
tasks, audits PRs, and recovers state, but never exercises judgment.
Judgment (mathematical review, merge decisions, replanning) belongs to
the orchestrator agent on the overseer's machine; see
`docs/agents/ORCHESTRATOR.md`.

The workflows stay thin (parse event, call into here, post result) so
this logic is unit-testable independent of any workflow and portable
to a different trigger surface if one is ever needed.
"""
