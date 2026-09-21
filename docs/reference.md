# Reference

A product-level reference for an overseer deciding whether to adopt Choir,
or picking policy at project start: every `.choir/*.toml` knob, every
label `scripts/setup-labels.sh` creates, and every gate check, in one
place. It documents what a knob does, never why the gate is shaped the
way it is — for that, read the tech report. For the day-to-day
playbooks, see `docs/agents/ORCHESTRATOR.md` and
`docs/agents/CONTRIBUTOR.md`.

## Configuration

Three files under `.choir/` in the project repo.

### `.choir/project.toml`

Read by three different parties, one section each: `[project]` by the
gate (from the PR's base SHA) and by workers (from their pinned commit);
`[automation]` by the orchestrator at loop start; `[reconcile]` by the
gate's daily reconcile workflow.

| Key | Accepted values | Default | Effect |
|---|---|---|---|
| `[project] prover` | `"lean4"` \| `"isabelle"` \| `"rocq"` | `lean4` (absent) | Selects the prover profile — trust vocabulary, placeholder tokens, declaration syntax. An explicit but unrecognized value, or malformed TOML, raises rather than silently defaulting: prover selection decides which audits run at all. |
| `[project] choir_protocol` | integer | `1` (absent) | The repo's pinned wire-protocol version. Written by the bootstrap and upgrade scripts; not meant to be hand-edited. |
| `[project] choir_commit` | string (git SHA) | none | Provenance only — records which Choir commit wrote the pin. Not read for behavior. |
| `[automation] merge` | `"auto"` \| `"approve"` \| `"manual"` | `auto` | Who presses the merge button. `auto`: the orchestrator reviews and merges green PRs itself. `approve`: the orchestrator reviews and posts a recommendation; a human confirms. `manual`: the orchestrator only observes and reports. The gate's audits and required checks apply identically at every level — this knob changes who acts on a green PR, never what must be true before one exists. |
| `[reconcile] stale_after_days` | integer ≥ 1 | `7` | How many days a claim may go without a heartbeat before the daily reconcile workflow reclaims it back to `choir/available`. |

### `.choir/verify.toml`

Read by the gate's verify workflows from the PR's **base** commit, never
the head — so a PR cannot loosen its own audit policy by editing this
file in the same diff.

| Key | Accepted values | Default | Effect |
|---|---|---|---|
| `[audits.axiom_honesty] policy` | `"net_zero"` \| `"whitelist"` | `net_zero` | `net_zero`: the PR must not increase the count of axioms/`unsafe`/`partial`/`native_decide`/`extern` (or the prover's equivalent patterns) relative to base. `whitelist`: an `axiom NAME` declaration must have its name in `allowed_axioms` to pass, regardless of whether it was already in base; the other watched patterns stay net-zero. |
| `[audits.axiom_honesty] allowed_axioms` | list of strings | `[]` | The names permitted under the `whitelist` policy. |
| `[audits.sorry_delta] policy` | `"block"` \| `"report"` | `block` | `block`: net-new `sorry`s (or the prover's placeholder tokens) fail the check. `report`: net-new placeholders are listed in the check output but the check passes — blueprint-style partial progress can merge on the orchestrator's review, and merged placeholders enter the trust inventory as open obligations. |
| `[audits.style] threshold_lines` | integer ≥ 1 | `200` | Max lines from a top-level declaration to the next before `verify-style` flags it. Advisory only — see Checks below. |

### `.choir/pins.toml`

Read by `choir worker claim` at workspace setup. Every check here is advisory —
a mismatch warns the contributor but never blocks the claim, and actual
breakage is still caught by the verify pipeline at PR-build time.

| Key | Accepted values | Default | Effect |
|---|---|---|---|
| `[pins] lean4_skills` | string (version tag) | unset | The `lean4-skills` version the project expects contributors to have installed. Unset means no check is attempted. |
| `[skill_pack] version` | string | unset | Version of the project's own skill pack (`skills/`). Reserved for future cross-workspace consistency checks; not yet enforced. |
| `[tools] mathlib_search` | `"off"` \| anything else / unset | unset (reads as "recommended") | `"off"` suppresses the one-time reminder a contributor gets at `choir worker claim` if they haven't declared a Mathlib search tool in their own `~/.choir/config.json`. Any other value, or the key's absence, keeps the reminder active. |

## Labels

Names are taken from `scripts/setup-labels.sh`, the script that creates
them on a project repo.

**Lifecycle** — mutually exclusive; an issue carries exactly one of these
at a time:

| Label | Meaning |
|---|---|
| `choir/available` | Available for claim |
| `choir/claimed` | Currently claimed |
| `choir/in-review` | Under review |
| `choir/done` | Completed and merged |
| `choir/invalid` | Failed intake validation |

**Identity / type:**

| Label | Meaning |
|---|---|
| `choir/task` | Choir-managed task, in any state |
| `choir/type:prove` | Task type: prove |
| `choir/type:golf` | Task type: golf |
| `choir/type:index` | Task type: index |

**Priority / difficulty** — orchestrator-owned; absence means normal
priority / unrated difficulty:

| Label | Meaning |
|---|---|
| `choir/priority:high` | Claim me first |
| `choir/priority:low` | Claim me last |
| `choir/difficulty:easy` | Estimated difficulty: easy |
| `choir/difficulty:medium` | Estimated difficulty: medium |
| `choir/difficulty:hard` | Estimated difficulty: hard |

## Checks

Every PR runs the checks below. Two classes decide what a *failure* does:
`Trust` and `Quality` both block a merge; `Advisory` checks report and
never block. A check that never runs at all on a given PR (for example
because the PR's head branch deleted its workflow file) counts as an
absent required check, not a passing one, wherever "Required present" is
`Yes` below.

The Class column is the base registry class (`gate/checks.py`'s `CHECKS`).
Where a project's prover relaxes that class (`PROVER_OVERRIDES`), the
override is named in the same cell — there are exactly two such
relaxations in the whole registry, both noted below.

| Check | What it verifies | Class | Required present |
|---|---|---|---|
| `rebuild` | The head branch builds cleanly from source — a clean-room compile; a contributor's own local build is informational only. | Quality | Yes |
| `statement-equiv` | String-normalized comparison of the target declaration's signature between base and head, so a PR can't silently weaken the statement it claims to prove. `UNDETERMINED` (the extractor couldn't isolate the signature) passes. | Trust (advisory on lean4) | Yes |
| `statement-immutability` | Every declaration already present in a changed file's base version reaches head with its statement untouched — or, for a declaration kind with no separable proof body, its whole declaration untouched. `UNDETERMINED` fails here, the inverse of `statement-equiv`. | Trust (advisory on rocq) | Yes |
| `axiom-honesty` | Net-new axioms / `unsafe` / `partial` / `native_decide` / `extern` (or the prover's equivalent trust vocabulary) relative to base, per `[audits.axiom_honesty]`. | Trust | Yes |
| `sorry-delta` | Net-new `sorry`s (or the prover's placeholder tokens) in changed files relative to base, per `[audits.sorry_delta]`. | Trust | Yes |
| `decide-instance` | Net-new `Decidable` instance declarations or `Classical.*` usage — a lean4-specific concept. The workflow is installed on every prover, but on isabelle/rocq the check has nothing to look for and always passes. | Quality | Yes |
| `comparator` | Kernel-level replay (via `leanprover/comparator`) that the head tree proves exactly the base statement's whole dependency closure, using only permitted axioms. Functions only on lean4 at toolchain ≥ v4.27; the workflow is installed on every prover but reports a not-applicable pass everywhere else (isabelle, rocq, and lean4 below v4.27). | Trust | Yes |
| `style` | Lines from a top-level declaration to the next, against `[audits.style] threshold_lines`. Reports only. | Advisory | No |
| `trust-report` | A post-build query of the built environment for a declaration's real axiom/oracle dependencies — `#print axioms` on lean4, `Print Assumptions` on rocq, kernel oracle-tracking on isabelle. Runs on every prover; lean4's probe is live-validated, isabelle's and rocq's ship the same invocation but await a live project to validate against. Reports only. | Advisory | No |

`review` is a registry entry with no live check behind it: no project
installs its workflow. It is kept so that a repository still carrying an
orphaned red `review` check reads it as advisory rather than as an
unrecognized (and therefore merge-blocking) name. It is not something an
overseer configures.
