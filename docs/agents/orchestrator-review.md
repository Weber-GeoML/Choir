# Orchestrator — review

Read this when a PR's gate is red, or reports partial progress: reading
a red `verify-comparator`, what to do with a `choir-defect` comment from
a blocked worker, and how to salvage a failed PR by failure type. The
green path — what a passing gate proves, the stage-two review checklist,
and the merge decision — stays in `ORCHESTRATOR.md`'s step 2.

### Reading a red `verify-comparator`

Read the report's named outcome, not the bare exit code.

- **statement-mismatch or illegal-axiom** are audit failures the PR
  owns: kernel-level evidence of a trust failure. Weigh them as you
  would the checks they stand in for (§ Salvaging a failed PR).
  `merge_pr`'s preflight already refuses the merge for you; what still
  needs your judgment is triage, below.
- **solution-build-failed** means a build inside comparator's own
  workspace died. **Check `rebuild` first.** If `rebuild` is also red,
  that check owns the diagnostics — one failure, not two. If `rebuild`
  is **green**, the fault is in comparator's generated workspace (the
  `ChoirBase/` challenge tree, or a from-source Mathlib build) — escalate
  it as an infrastructure fault, not a duplicate to dismiss.
- **sandbox-unavailable, tool-error, or missing-constant** mean
  comparator verified *nothing*. These block the merge too — the
  preflight can't tell "unverifiable" from "failed" — but never treat
  any of them as grounds to reject a contributor: escalate to the
  overseer as an infrastructure fault instead. `missing-constant` means
  the target declaration is absent from comparator's base-built
  challenge environment, which the contributor's diff cannot change —
  it points at a mis-scoped task or at Choir's own workspace generation.

`comparator` is `CheckClass.TRUST` and required-present, so `merge_pr`'s
preflight refuses a comparator-red PR without you doing anything —
including the case comparator exists to catch: a worker leaving the
target statement text-identical while silently redefining a shared `def`
in its dependency closure, with every other check green.

What the mechanism can't do is read *which* outcome fired: `comparator`
is `gate.checks.SALVAGE_OPAQUE`, one name spanning outcomes from unsound
to reusable to unverifiable. **Read the report before deciding what to
do with the branch** — that judgment is still yours (§ Salvaging a
failed PR).

### Partial progress

A hard task may come back 70% done: real intermediate lemmas, with a
sorry or two left. What happens next follows the project's sorry policy
(`.choir/verify.toml`, `[audits.sorry_delta]`):

- **`block` (default).** The PR can't merge. Don't waste the work:
  commit the useful lemmas yourself as `sorry`-bearing declarations and
  publish the remainder as `prove` tasks, or tell the worker which green
  subset to resubmit.
- **`report`.** The check passes and lists the net-new sorries. Merging
  is your decision, not automatic. Merged sorries land in the trust
  inventory as open obligations for your next planning pass.

### A reduction

A PR may declare a `choir-reduction` block: it proves the target and names open
obligations it leans on. The gate has checked that the target is proved, that
new placeholders sit only in declarations the block declares and the file didn't
already have, and that `parent` is this task's target. Don't re-derive that.

Check four things:

- Does each child statement say the right thing?
- Is each child closable in one PR?
- Is any child an interface other groups will build on? A shared definition is
  not the worker's to add — reject and author it yourself.
- Is the split real? A target proved by one child that restates it is valid and
  empty.

Then:

- **Accept** — merge, run `choir orch sync-graph <checkout>` so each child the
  PR introduced becomes a node, give each the group its parent holds, publish
  a `prove` task per node, and account for the new nodes in that group's
  prose. The group is inherited, not chosen: a child lives in the target's
  file and is part of the same result. A child naming an obligation already in
  the corpus needs none of this; it may already have a task.
- **Amend** — merge, then commit the reshaped child yourself and record it in
  the graph. The build tells you whether the parent still composes.
- **Reject** — close with the reason and write the lesson to
  `skills/orchestrator-notes.md`.

Never publish child tasks from a reduction whose gate is red.

Record the reduction's PR number and the nodes it created in the roadmap's log.

Children must live in the target's file. Don't accept a split whose obligations
belong in another file.

A reduction can be refused for a name collision: a child entry is disarmed by
any declaration in the file sharing its last name segment. The worker's fix is
to rename the obligation.

A red `sorry-delta` on a PR carrying a block most often means the target is
still unproved, or a placeholder landed on a declaration the file already had —
both refused under the reduction contract. A malformed block, or a `parent` that
doesn't match the task's target, instead falls back to the ordinary contract.
The check prints which.

### A `choir-defect` comment from a blocked worker

**A worker that believes a check is wrong says so in a comment rather
than working around it, and reading these is part of your loop.** This
is the escape hatch that makes a nonzero false-block rate on
`statement-immutability` tolerable: every false block is one comment
away from you resolving it.

The worker distinguishes two claims, because each needs a different
answer from you:

- **"This statement is wrong."** A defect in the task, not the work.
  Verify it yourself — you authored the statement and have the plan
  context the worker lacks. If they're right, fix the statement on the
  protected branch, update the plan, and re-publish; if not, say so on
  the issue.
- **"This proof is hard."** A progress report, not a defect. It feeds
  the struggle signal and your decomposition decision, not a change to
  the statement.

It is **evidence, not a command**: a worker never unilaterally halts a
task, so the comment does not release the lease, close the issue, or
change a label. It annotates; you adjudicate.

Two false-positive shapes worth recognizing before you assume tampering:
two declarations sharing a name (reported `UNDETERMINED`, because the
check genuinely cannot tell which one moved), and a new helper landing
next to an unusual top-level command. Neither is the worker touching a
statement.

### Salvaging a failed PR — triage by failure type

**Never merge a PR with a red *blocking* check** — the gate is the trust
floor — but don't waste the effort either. "Red" and "blocking" are not
the same set: `style` and `trust-report` are advisory, so they can be
red on a PR `merge_pr` will still merge. Read them, weigh them, merge
anyway if the mathematics is sound. `comparator` is blocking —
`merge_pr` refuses a comparator-red PR mechanically — but it is
`gate.checks.SALVAGE_OPAQUE`, so what still needs your judgment is
triage: read which outcome fired (§ Reading a red `verify-comparator`)
before acting on it.

Triage by *what* failed:

- **Trust failures** — a comparator statement-mismatch or illegal-axiom
  finding, a new axiom or `native_decide`, a smuggled hypothesis, a
  net-new sorry under `block`. On lean4 at v4.27 or above,
  `statement-equiv` is advisory for merging (comparator supersedes it
  there) and never reaches this list for a *merge* decision on its own —
  **but that relaxation is merge-axis only, and does not extend to
  salvage.** `classify_failure` takes no `prover` argument and always
  reads `statement-equiv`'s strict base class, on every prover: a
  `statement-equiv` `CHANGED` still classifies `TRUST`, never
  `NEAR_MISS`, even on lean4. A statement-weakening tree
  must never be auto-seeded as the next worker's starting point,
  regardless of what covers the merge decision. The code is unsound:
  **reject it, and if the task still needs doing, re-publish the
  original clean task — never seed a follow-up from the bad diff.**
- **A red `statement-equiv` on isabelle, rocq, or a lean4 project below
  comparator's v4.27 floor is a triage point, not an auto-reject.** None
  of these three has a kernel-level statement check, so blocking
  coverage differs by prover: on **rocq**, `statement-equiv`'s string
  comparison is the only blocking statement signal (`statement-immutability`
  is advisory there). On **isabelle**, and on a lean4 project below the
  floor, `statement-immutability` also blocks and independently compares
  the statement for any base-present, proof-bearing declaration — so
  `statement-equiv`'s red is one of two overlapping signals there, not
  the only one. Either way, its blocking is what forces you to look
  before deciding anything. Read the diff: is the change actually
  reasonable and necessary, or model hallucination?
  - **Reformatted, math untouched** (a renamed variable, a reordered
    hypothesis, whitespace): restore the statement byte-identical and
    resubmit the same proof — don't throw it away.
  - **Actually changed what's claimed** (a weakened hypothesis, a
    narrowed conclusion, a smuggled assumption): a trust failure like
    the ones above — reject, re-publish the clean task, never seed a
    follow-up from the bad diff. On lean4 at v4.27+ the analogous case
    is a comparator `statement-mismatch`, which needs no triage — the
    kernel has already told you.
- **A red `statement-immutability` stops the merge on lean4 and
  isabelle, and is a signal to read on rocq** (advisory there — its
  false-block rate is unmeasured). It fires whenever a declaration
  present in the base version of a changed file changed, was deleted, or
  couldn't be confirmed unchanged; it is not `SALVAGE_OPAQUE` — one red
  here means one thing. Its whitespace tolerance stops at the token
  boundary — an alpha-renamed binder still reads `CHANGED`, since under
  this check a worker touches no statement at all. But its enumeration
  is a heuristic, not a parser, so **read the finding before treating
  it as a real change.** Two shapes it over-reports on:
  - A command its trailer allowlist doesn't recognize, inserted right
    after an untouched declaration, can fold into that declaration's
    reported span. Read the two spans: if the preceding declaration's
    own text is otherwise unchanged, this is the false-block shape, not
    tampering.
  - A definition-bearing declaration whose body is legitimately a proof
    (a `Prop`-valued lean4 `instance`, a rocq `Example` written as a
    mini-theorem): golfing it reports `CHANGED`, because the
    definition-bearing comparison can't distinguish a data body from a
    proof body. Read the two spans: if only the tactic proof moved,
    this is a conservative report, not tampering.

  Once you've read the finding and it genuinely is a base declaration's
  text changed: treat it as a trust failure by your own judgment —
  reject, and if the task still needs doing, re-publish the original
  clean task, never the bad diff. That judgment call is yours to make
  and record in the roadmap's log; no check enforces it for you.
- **Near-miss / quality failures** — a failed `rebuild` (a comparator
  solution-build-failed alongside a **green** `rebuild` is Choir's own
  infrastructure fault, not the contributor's — escalate it instead of
  triaging it as a duplicate), or a sorry under `report`. The mathematics is
  usually sound; only the shape is off. **Salvage it: open a follow-up
  `prove` task that seeds the worker's branch (the almost-good code) and
  names the one thing to fix.** The next worker starts from 90%, not
  zero. Close the failing PR **without deleting its branch** — the seed
  commit must stay reachable. `style` is advisory and can never trigger
  salvage on its own — a long declaration is a signal you read, and the
  right vehicle for it is a `golf` task, never a salvage.
- **Comparator's cannot-verify outcomes are neither of the above.** They
  block the merge but say nothing about whether the code is sound or
  merely rough — escalate to the overseer, don't triage it as a
  contributor defect. `classify_failure` reaches the same `TRUST` answer
  when comparator is the only blocking failure present (fail-safe: never
  auto-reuse code nobody verified), **or the `rebuild`/`axiom-honesty`
  answer when another readable check is also red** — neither is the
  same as "the proof is unsound" or "the proof is a fixable near-miss."
  Read the report yourself before acting on whichever mechanical answer
  comes back.
- **Bound it.** A salvage task that fails the same way again is a stuck
  task — apply the `orchestrator-planning.md` § Stuck tasks threshold.
  Don't loop salvage tasks forever.

Automation, as everywhere: under `auto` you open the salvage task
yourself; under `approve`/`manual` you propose it. Length never blocks a
merge — `verify-style` reports and passes regardless of
`[audits.style] threshold_lines` — so honest-long math needs no action;
reach for a `golf` task only when the length is genuinely unwarranted.

```bash
# `prs` already reports missing_required_checks and blocking_failures.
#   missing covering everything -> no audit ran at all: NEVER salvage input
#   nothing blocking failed      -> review normally
# Otherwise classify. Salvage always reads strict, so `classify` takes no
# --prover: a check that is advisory for one prover must not quietly
# downgrade a trust failure.
choir orch --repo org/project classify \
    --failed-check comparator --failed-check sorry-delta

# NEAR_MISS -> carry the original task forward against the failed head:
choir orch --repo org/project salvage \
    --issue <n> --pr <pr> --head-sha <sha> --failed-check <name>
# TRUST -> reject; re-publish the original clean task, never the bad diff.
```

If `comparator` is among the failures, read the PR yourself before
trusting either branch — its name alone never says which outcome fired.

**These guards are load-bearing, not defensive noise.**
`classify_failure` is only meaningful on a non-empty blocking-failure
set. An empty one — including a PR refused for *missing* required
checks, which contributes no failing name at all — answers `NEAR_MISS`,
which is right about "is this code safe to reuse" and wrong about "does
this PR need a salvage task." Never treat that empty set as salvage
input: it would seed the next worker from a tree with an audit workflow
deleted.
