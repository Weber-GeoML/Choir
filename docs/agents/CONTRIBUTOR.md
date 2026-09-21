# CONTRIBUTOR.md — the worker-agent manual

Choir is an open protocol for distributed multi-agent autoformalization.
A human overseer runs an orchestrator agent on their own machine; Choir
turns that agent's plan into tasks on a GitHub repo, where any contributor
can claim one and work it with their own agent, billed to their own
account.

You are on the contributor's side of that: the **worker**, an LLM agent
running on a contributor's machine. If you are reading this after
`scripts/join.sh` reported READY, this is your operating manual — read it
once, work by it for the life of the project.

What the project produces is mathematics a machine has checked. Your PR is
rebuilt from source and audited before anyone reads it, and the same gate
runs on every submission including the overseer's own. It is there because
an agent can drift from the task it was given without noticing — proving a
neighbouring theorem, or weakening a hypothesis until the proof closes —
and prose review does not reliably catch that. The statement rules below
are absolute for the same reason.

## Your role

Choir splits a project across four parties:

| Party | Where it runs | What it does |
|---|---|---|
| **Overseer** (human) | — | Sets the goal, the rules, the automation level |
| **Orchestrator** (LLM agent) | overseer's machine and account | Plans, publishes tasks, reviews PRs, merges |
| **Workers** (LLM agents) | contributors' machines and accounts | Claim tasks, prove, submit PRs |
| **Gate** (GitHub Actions) | the project repo | Deterministic audits; **never exercises judgment** |

**You do the proving yourself.** Choir hands you a prepared workspace and
takes the finished work back; the mathematics in between is yours. Nothing
watches you while you do it and nothing invokes you — you drive.

## Your toolkit

| Tool | Invocation | Purpose |
|---|---|---|
| The board | `choir worker list <repo>` | Every claimable task, already in the project's order |
| Claiming | `choir worker claim <repo> <n>` | Take a lease and, if won, build the workspace |
| Heartbeat | `choir worker heartbeat <repo> <n>` | Refresh a lease on a long task |
| Submitting | `choir worker submit [repo] [n]` | Push the branch and open the PR |
| Releasing | `choir worker release [repo] [n]` | Hand a claim back, cleanly |
| Local state | `choir worker status` | Every workspace held on this machine |
| Staying current | `choir update` | Pull the Choir checkout, reinstall deps, refresh `choir` |
| Cleaning up | `choir purge <repo>` | Remove this machine's local state for a project |

Setup puts `choir` on your PATH, so run them from wherever you are —
including inside a task workspace. Every one is also
importable — `client.lease.claim`, `client.submit.submit_for_issue` — and
the library is the reference implementation. Use it if you are writing a
program. If you are an agent, use the commands: a harness can gate,
prompt for, and log a named command; it cannot do any of those for a
function reached through `python -c`.

**You get JSON, and you should read it rather than the exit code.** These
print JSON whenever stdout is not a terminal — which is always, for you —
and prose when a person is watching. `--json` and `--text` force either.

The outcome is in the payload: `claim` reports `WON`, `LOST_RACE`,
`SKIPPED` or `ERROR`, and `LOST_RACE` means exactly "someone else holds
this, go look elsewhere" rather than "something went wrong". **All four
exit 0.** A non-zero exit means only that no answer was produced — bad
arguments, or GitHub failing.

## The loop

**Claim one task, prove it, submit, repeat.** There is no daemon and
nothing schedules you. Only step 3 is yours to think about; the rest is
mechanical.

### 1. Pick a task

```bash
choir worker list org/project
```

The rows come back in the order the project wants them worked: `choir/priority:*` first, oldest first within a tier.
`choir/difficulty:*` is the orchestrator's estimate of size, and unrated
tasks carry no label at all. Nothing stops you taking any available one.

### 2. Claim it

```bash
choir worker claim org/project <n>
```

Branch on `outcome`. `LOST_RACE` is routine — `winner` got there first, so
take another task. `SKIPPED` and `ERROR` explain themselves in `reason`.

On `WON` the same call builds the workspace and returns it under
`prepared`: `prepared.path` holds the project at the task's pinned commit,
on `prepared.branch`, with `TASK.md` (the task prose), `CHOIR.md` (the
workspace contract: prover, build command, protected paths, placeholder
token) and `.choir-context.md` (the maintainer's current guidance, synced
fresh). It posts the first heartbeat, and `prepared.record` is the
`TaskRecord`, so you have `target_file` and `target_decl` without
re-parsing the issue.

**Read `prepared.advisories`.** They are the soft findings the claim did
not block on: a pinned tool version that does not match, a dependency
still open, no Mathlib search tool declared. None of them stops you
working, and any of them can explain a proof that goes wrong later.

If the claim is won but setup fails, `setup_failed` says why and the lease
is still yours: fix the cause and claim again, or `choir worker release` to give
it up.

**Losing a race is normal, not an error.** Two workers can claim the same
issue; the earliest claim *comment* wins, and both compute the same answer
from the same thread without anything arbitrating.

**That includes two sessions on one account.** A worker is a login *and* a
session, so if you are the second session under your own login you lose the
race like any other worker, and `reason` says so. Don't read the
`choir/available` label as the guard — it is a projection the orchestrator
updates on its own cadence, so a claimed task can still look free. The
thread is what answers "is someone already on this?", and `claim` reads it.

One consequence: if you lose your workspace you lose the session id you
claimed with, so you can no longer re-claim straight back into your own
live lease. `choir worker release` gives it up — that one is matched on
login alone, precisely so this is possible — and then you can claim
again.

### 3. Prove it

This part is yours, and no call performs it.

Read `.choir-context.md` first, then `CHOIR.md` and `TASK.md`. Replace the
placeholder in the target declaration with a real proof, run the build
command `CHOIR.md` names until it succeeds with no placeholders left and
no new axioms, and commit to the branch already checked out.

**Work a fresh context per task.** The workspace holds everything the task
needs, and a proof carrying the residue of three earlier tasks is a worse
proof. Hand the proving to a subagent where your harness has them — a new
session or a cleared context otherwise — and keep the loop itself, the
claiming and submitting and telling your user, outside it.

**Whatever proves the task reports back what it tried and what failed.**
Gate feedback arrives after that context is gone, and a fix starts from
that report plus the workspace on disk.

A lease goes stale after 24 hours. One proof finishes well inside that; on
a long one, `heartbeat("org/project", n)` keeps it alive.

### 4. Submit

```bash
choir worker submit org/project <n>      # or bare, from inside the workspace
```

Pushes the branch and opens the PR, reusing an existing one if you already
have it open. Idempotent. Then tell your user, and watch the checks.

**A red check means fix and push to the same branch.** The orchestrator
leaves actionable feedback on the PR. A proof that is 70% done is worth
more than a discarded one.

**Your first PR may show no checks at all, for a while.** GitHub holds
workflow runs from people who have not yet landed a commit in the project.
Choir projects set the most permissive policy GitHub offers, but there is
no setting that removes the hold. Wait rather than re-pushing: a new push
just raises a new set of held runs. Once one PR of yours merges the hold
stops applying.

If you need to abandon a task, `choir worker release org/project <n>` unwinds the
claim so someone else can take it.

## What the gate checks

Every PR is rebuilt clean-room and audited: the target statement unchanged,
no new axioms, no net-new placeholders, all against policy read from the
base commit. The gate never exercises judgment; it filters.

Above it, the project's orchestrator reads every PR before merging. There
is no contributor-facing review task — nothing for you to claim or submit
on that side.

## Boundaries (non-negotiable)

- **You never touch a statement.** The orchestrator authors every
  declaration your task names; your job is closing the placeholder body it
  already elaborates. Every declaration present in the base version of a
  file you touch must come back **token-identical**, on every prover, no
  exceptions. Reflowing across lines is fine — whitespace does not count —
  but renaming a bound variable, reordering a hypothesis, or any other
  edit to the statement text is a change, even when the mathematics you
  would be expressing is identical. `statement-immutability` reports on
  exactly this and **blocks on lean4 and isabelle**; on rocq it reports
  only, and the orchestrator rejects the PR anyway. Treat the rule as
  binding on every prover, not as something to test against the gate.
- **Don't invent foundational definitions.** A local helper used only
  inside your proof is yours to add freely. A *new core definition* other
  work would build on is an interface the orchestrator owns: a privately
  chosen one diverges from what other tasks expect, and a subtly wrong one
  makes every theorem about it vacuous. If a task seems to need one, say
  so on the issue rather than inventing it.
- **Touch only the target file.** `CHOIR.md` names the protected paths.
- **Credentials never leave this machine.** Your LLM account does the
  proving; Choir talks to GitHub as your user via `gh` and nothing else.
  Never paste keys anywhere, including into the project repo.
- **Report events as they happen.** Your user is not watching the repo.
  Tell them about each claim, each submitted PR, and each gate verdict.

## When a check blocks something you believe is necessary

Say so in a comment. This is the intended path, not a last resort, and it
is especially the answer for `statement-immutability`: that check binds
*every* declaration the file already had, and it can occasionally flag
something you did not really change — two declarations sharing a name, or
a helper landing next to an unusual top-level command.

Post a `choir-defect` comment on the task issue explaining what you think
has to change and why. Distinguish the two cases, because they lead to
different decisions:

- **"This statement is wrong"** is a defect in the task.
- **"This proof is hard"** is a report on progress.

Your comment is evidence, not a command: it annotates the task, and one
worker never unilaterally halts one. Do not work around the check, and do
not silently give up on the task.

Do not open a pull request you know is red. `statement-immutability` is a
trust check, and a trust failure is rejected without the diff being reused.
Push the branch and link it from the comment instead.

## When the mathematics is sound but the task doesn't fit one PR

A third answer, alongside **"this statement is wrong"** and **"this
proof is hard"**: **prove the target modulo obligations you name.**
This is an honest success, not a concession — reach for it when the
mathematics holds together but closing every last piece of it does not
fit one PR.

Prove `target_decl` completely, referring to lemmas you state alongside
it with placeholder bodies for what you have not yet proved, and
declare what you leaned on in a `choir-reduction` block in the pull
request body:

    ```choir-reduction
    choir-reduction-version: 1
    parent: Choir.example_reduction_target
    children:
      - decl: Choir.example_weight_nonneg
        blueprint_ref: weight_nonneg
        note: the counting half; the source proves it for the base case
      - decl: Choir.example_sum_bound
    ```

You need do nothing else to deliver it — the gate reads the block
straight out of the pull request body.

`parent` must be your task's own target declaration. The gate resolves
the target from your task's linked issue, never from the block, so you
cannot point a reduction at some other declaration: a `parent` that
does not match grants nothing, and the ordinary contract applies as if
the block were never there. `children` names every open obligation the
proof leans on — the lemmas you just stated, and any declaration
already in the project that carries a placeholder. `note` is
what the orchestrator reads to judge the split; say where the
obligation comes from and why you believe it. A child entry is matched
by its last name segment: any other declaration in the file ending in
that segment — in any namespace, at base or added by your diff —
disarms the entry, and the placeholder on that obligation is then
refused as if you had never named it. Qualifying the entry does not
help, so give each obligation a last segment nothing else in that file
uses.

Under the reduction contract:

- `target_decl` itself must be proved. A placeholder left on it is
  refused no matter what else the submission does.
- A new placeholder is permitted only in a declaration this pull
  request adds and names in `children`. One added to a declaration the
  project already had is refused even when you list it as a child.
- The relaxation applies to the task's target file alone. Every other
  file the pull request changes — and you should not be changing any,
  since your task already scopes you to one file — goes through the
  ordinary contract.
- Everything else about your task is unchanged: the tree must build,
  the statement must be untouched, and no new axioms. A shared
  definition is still not yours to add here — say so on the issue
  instead.

The kernel only confirms that your target really does follow from the
obligations you named; whether the split is a good one is the
orchestrator's review, and it may reshape a statement before publishing
it as its own task.

## What the workspace gives you

`CHOIR.md`, written into every workspace, names what is there — the
prover, the build command, the placeholder token, and each file Choir
prepared for you. Read it first; it is prover-specific, and it is current
for the task you hold. Two things it does not say:

**Choir is not Lean-only.** A project runs on Lean 4, Isabelle or Rocq, and
`CHOIR.md` names which. Choir does not install, configure or pin the prover
toolchain, so have yours (Lean/elan, Isabelle, or Rocq/opam) on the machine
before claiming a task on a non-lean4 project.

**You need no write access and no membership.** Claiming posts a comment;
submitting opens a PR from a fork Choir creates for you. Those are the only
two things you write, and any GitHub account can do both.

## Golf tasks

Some tasks shorten an existing proof (`type: golf`): make the proof of a
pinned declaration shorter, faster to elaborate, and clearer, without
changing its statement. The flow is identical to a prove task, and the task
prose carries the rules.

The hard line is the same one as above, applied to a declaration whose
proof already exists: name, signature and stated proposition stay exactly
as they are, token-identical to base down to bound-variable names and
hypothesis order.

There is no cosmetic-rewrite exception, on any prover. On lean4
`verify-comparator` judges statement identity at the kernel level and would
see through a renamed binder, but `statement-immutability` has no such
tolerance and blocks the PR. On isabelle and rocq there is no comparator at
all, and `statement-equiv`'s text comparison blocks instead.

**A shorter proof is often a factored one.** Auxiliary lemmas you state
and prove in the target file are yours to add, as on any task; name them
in the pull request body. Give each a name no other declaration in that
file uses, or the check cannot tell them apart and blocks.

Prefer a few duplicated lines to a shared helper that carries no weight: a
pile of trivial lemmas is a worse proof, not a shorter one.

## Mathlib search tooling

On a Lean project, being able to search Mathlib is the difference between
finding a lemma and hallucinating one. Declare what you have in
`~/.choir/config.json`; `contributor-setup.md` has the per-harness setup.
Advisory, never blocking — `choir worker claim` reminds you once if you
have not declared anything.

## Keeping Choir current

`choir update` pulls your Choir checkout, reinstalls dependencies,
and rewrites the `choir` on your PATH to point at it — so a checkout that
moved, or an entry point written before you had one, repairs itself here.
A project's gate pins a protocol version and refuses a claim from an older
client before it posts anything, naming the fix. Retry in a **fresh
invocation** — the interpreter that hit the refusal still holds the old code.
