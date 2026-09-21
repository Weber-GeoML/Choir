"""Periodic reconciliation: orphan-lease detection and recovery.

A scheduled workflow runs `gate.reconcile.cli` daily. It finds
issues that are still `choir/claimed` long after their last heartbeat
(or, for issues without any heartbeat label, long after `updated_at`)
and demotes them back to `choir/available` so other contributors can
pick them up.

The pure decision logic lives in `stale_claims.py`; the CLI applies
the resulting actions via `gh`.
"""
