# Project workflow templates

These GitHub Actions workflows are copied into every Choir-managed project's
`.github/workflows/` by `scripts/new-project.sh` (and refreshed by
`scripts/upgrade-project.sh`) — the gate's per-project audits (`verify-sorry`,
`verify-statement-equiv` (blocking on isabelle/rocq, advisory on lean4 —
`gate/checks.py`'s `PROVER_OVERRIDES`, design note 14 §8),
`verify-statement-immutability` (advisory on every prover by decision, not
by schedule — spec D2, design note 14 §8 item 5), `verify-axiom-honesty`, `verify-style`, `verify-decide-instance`,
and the merge-blocking `verify-comparator`, lean4-only — design note 14 §8)
and lifecycle jobs (`issue-intake`, `issue-close-on-merge`, `reconcile`).

They live here, **not** in this repo's own `.github/workflows/`, on purpose:
the gate is meant to run on PRs to Choir-*managed projects*, not on PRs to the
Choir framework itself. Keeping them out of the active workflow directory means
they don't run on this repo's own PRs — which would otherwise block on, e.g.,
the reference samples' intentional demo `sorry`/`Admitted` (those are the
canonical first-task targets, so the block-mode `sorry-delta` audit would flag
any PR that touches them).

Choir's own CI stays in `.github/workflows/`: `ci.yml` (ruff + pytest) plus
`verify-pr.yml` / `verify-trust-report.yml`, which dogfood the reference
`samples/lean4` build. Those two are generated per-prover by the bootstrap
scripts for real projects (heredocs), so they are not templated from here.
