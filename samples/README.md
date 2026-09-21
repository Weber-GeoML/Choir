# samples/ — internal fixtures, not the project template

These reference projects (`lean4/`, `isabelle/`, `rocq/`) are **Choir-repo
internal**: nothing in them ships to users, and bootstrapped projects copy
nothing from here.

What users actually receive comes from elsewhere — `new-project.sh`
generates a fresh skeleton (toolchain pinned to the latest stable *at
creation time*, or `--toolchain`/`--from` overrides), gate workflows are
copied from `templates/workflows/`, and the Python packages from
`gate/` / `orchestrator/` / `client/`.

What the samples are for:

- **CI dogfood.** `verify-pr.yml` and `verify-trust-report.yml` build
  `samples/lean4` on every PR to this repo — the only place the gate's
  prover-facing code paths meet a real project before a user's does.
- **Live-test fixtures.** The lean4 trust-report integration tests (and
  future comparator smoke tests) run against it where a toolchain exists;
  the isabelle/rocq samples are the structural-parity fixtures (live
  validation pending).
- **Demo stage.** The intentional `sorry` on `one_add_one` in
  `samples/lean4` is the canonical demo task a worker closes.
- **Browsable reference** of what a bootstrapped project looks like
  (`.choir/` policy files, the example `skills/` pack).

Toolchain policy: the pins stay **concrete versions** (a floating channel
would break reproducible CI and comparator's version-derived tag, note 14
§6) and are bumped **opportunistically** — only when a feature floor
demands it (e.g. comparator's v4.27) — via a PR so `verify-pr / rebuild`
validates the bump. No scheduled auto-bumps; staleness here affects no
user.
