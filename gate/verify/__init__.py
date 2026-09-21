"""Phase 3 verify audits.

Each audit is its own module + workflow + status check. Branch
protection on the project repo requires all the relevant checks to be
green for `main` to accept the merge.

v0 ships clean-room rebuild (in `.github/workflows/verify-pr.yml`,
shell-only) and statement-equivalence (this package). Future audits:
axiom-honesty, decide-instance, style — each its own sibling module.

The pure decision logic lives in module top-level functions; the CLI
in `*_cli.py` fetches data from gh/git and applies the logic. Same
shape as `gate.state` and `gate.reconcile`.
"""
