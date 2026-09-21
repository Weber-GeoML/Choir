# Orchestrator — planning

Read this when deciding what to publish next or how to handle a
struggling task. The loop itself is in `ORCHESTRATOR.md`.

## The roadmap

`roadmap/` in the project repo holds the plan. Three parts:

| Path | What it holds |
|---|---|
| `roadmap/README.md` | the roadmap itself — goal, route, what comes next, and your reasoning log |
| `roadmap/<group>.md` | the mathematics of one group of related results |
| `roadmap/graph.json` | the dependency graph and what is finished |

Write the prose first and get the decomposition right there; the graph
records it afterwards.

### `roadmap/README.md` — the roadmap

What the project is formalizing, the route you have chosen through it, and
which groups come in which order, linking each group file. Keep it short
enough to re-read at the start of every session.

It also carries your **reasoning log**: what you decided and why — a PR
merged or rejected and on what grounds, a task decomposed, a shared
definition rewritten, anything escalated to the overseer.

**Compress the log every loop.** It is a working record, not an archive.
Each entry either still constrains what you do next or it does not:

- A decision that changed the plan — fold it into the route above and
  delete the entry.
- A failure mode workers keep hitting — move it to
  `skills/orchestrator-notes.md` and delete the entry.
- A question the overseer has answered — keep the answer, drop the
  exchange.
- A routine merge — no entry at all.

Keep open escalations, decisions still binding future work, and anything a
replacement orchestrator would otherwise have to re-derive.

### `roadmap/<group>.md` — the mathematics

One file per group of related results. Say what the group establishes and
what each node claims, in ordinary mathematical prose with the sources it
comes from. Group by mathematical significance, not by the size of a
source section.

After ratifying a reduction, this file must say what the new nodes claim and
why the split has the shape it does. How is yours: fold three children into a
sentence, rewrite the section, or reorganise the group if the reductions
revealed a better structure. A worker's `note` is raw material, not text to
paste.

Reductions coming back are evidence about the plan. A group whose splits keep
landing the same way was organised wrong.

### `roadmap/graph.json` — the graph

```json
{
  "target": "<the source this project formalizes>",
  "nodes": {
    "main_estimate": {
      "kind": "group",
      "doc": "main-estimate.md"
    },
    "sum_range_succ": {
      "kind": "theorem",
      "parent": "main_estimate",
      "decl": "Finset.sum_range_succ",
      "upstream": true
    },
    "weight": {
      "kind": "definition",
      "parent": "main_estimate",
      "file": "Project/Core.lean",
      "decl": "Project.weight",
      "statement": "formalized",
      "proof": "formalized",
      "uses": [],
      "proof_uses": []
    },
    "weight_nonneg": {
      "kind": "theorem",
      "parent": "main_estimate",
      "file": "Project/Core.lean",
      "decl": "Project.weight_nonneg",
      "doc": "main-estimate.md#weight-nonneg",
      "statement": "formalized",
      "proof": "formalized",
      "uses": ["weight"],
      "proof_uses": ["sum_range_succ"]
    },
    "main_bound": {
      "kind": "theorem",
      "parent": "main_estimate",
      "file": "Project/Core.lean",
      "decl": "Project.main_bound",
      "statement": "formalized",
      "proof": "planned",
      "uses": [],
      "proof_uses": ["weight_nonneg"]
    }
  }
}
```

| Field | Meaning |
|---|---|
| `kind` | `theorem`, `definition`, or `group` — a container holding other nodes |
| `parent` | the group this node sits in; a group may sit in another group |
| `file` | the file the declaration lives in — you cannot publish a task without it |
| `decl` | the declaration's name |
| `doc` | this node's prose, relative to `roadmap/`; omit it for `<parent>.md#<id>` |
| `statement` | `planned` until the declaration is committed, then `formalized` |
| `proof` | `planned` until the proof — or a definition's body — lands, then `formalized` |
| `uses` | nodes this node's **statement** needs — derived once formalized |
| `proof_uses` | nodes only its **proof** needs — derived once formalized |
| `not_ready` | set when a node must not be worked yet — an imminent replan |
| `upstream` | set when the result already exists in the prover's library; needs no `file` |

The graph has two halves, kept two different ways.

**Derived — run the tool, don't hand-edit.** Every declaration the
project's source declares gets a node, and every formalized node's `uses`
and `proof_uses` are the edges its term actually has.

    choir orch sync-graph <checkout>

Run it once a pass, on the checkout you pulled and rebuilt after the
pass's merges landed — not once per merge, which would read a build that
does not contain them yet. A helper a contributor
proved inside their own pull request is a declaration like any other: it
is what later work reuses, and a plan that omits it draws the theorem
above it as resting on nothing — and reads a node with no recorded
dependencies as complete, because there is nothing under it left to
check.

**Yours — nothing can derive it.** Group membership (`parent`), prose
(`doc`), the statuses, `upstream`, `not_ready`, and every edge no term
can answer for:

- a node whose `statement` is still `planned` — there is no term at all;
- the `proof_uses` of a node still carrying a placeholder — the route
  you intend to take, written before the proof that will justify it.
  Its `uses` is derived, because the statement's type is already there;
- any edge to an `upstream` node — `sync-graph` reports the project's
  own declarations, so a dependency on a named library result is yours
  to record and is carried across untouched.

Write these and `sync-graph` keeps them. Everything else it rewrites.

It reflects the source and never argues with it. Where the plan claims
something the source contradicts — a `formalized` node whose declaration
is gone, or a `planned` one that is already written — it reports the node
under `disputed` and changes nothing: which way to correct it is a
judgement about what you meant, and a tool that guessed would be editing
the plan rather than reading it.

A node is not the same thing as a task. A *task* is a node you publish,
and those stay one to one: when a node turns out to be too large to prove
in one PR, split it and give each part its own node. Nodes with no task
are ordinary — every helper is one.

### Keeping it current

Write `statement` and `proof` once, when the work lands.

- Set `statement: formalized` when you commit the declaration.
- Set `proof: formalized` when the PR filling it merges. Under sorry
  policy `report` a PR merges with its placeholder still in place, so
  confirm against `choir orch inventory scan <checkout>` before
  recording it.
- A node `sync-graph` mints is `formalized` already, with `proof` read
  off the term rather than the source. It never rewrites a status you
  wrote.
- Retract a status when the work is reverted or the statement is rewritten.
- Never write readiness, blocked, or done into the file. Read those off
  the graph when you need them.

### What to publish

A node is ready to publish as a `prove` task when all three hold:

- `statement` is `formalized`,
- `proof` is `planned`,
- every id in `uses` and `proof_uses` has `statement: formalized` or
  `upstream: true`.

A dependency needs to be **stated**, not proved — nodes fill in any order.

A node whose `statement` is still `planned`, and all of whose `uses` are
stated, is yours to write: commit the declaration with a placeholder body
before publishing anything against it.

The node's `file` and `decl` are the task's `target_file` and
`target_decl`; set `blueprint_ref` to the node's id.

Keep a node's dependencies out of the task's `deps`, which holds issue
numbers and records which task this one was split out of.

## Decomposition guidance

- **Decompose shallowly, and publish all of it.** State the skeleton
  that carries the mathematical structure — the results the project is
  for, and what they visibly rest on — and publish every node of it that
  is ready. Don't build depth yourself: depth arrives as reductions come
  back, and each one you accept adds the nodes below it. A deep tree
  written upfront multiplies wasted worker compute when the
  decomposition is wrong, and there is no longer any reason to write
  one.
- A sorry that survives several failed attempts is a signal to split
  it: state the intermediate lemmas as their own `sorry`-bearing
  declarations, commit them yourself, add a node for each, and publish
  each as a `prove` task. Point the original node's `proof_uses` at
  them.
- **You author every statement a `prove` task targets.** The
  placeholder body already committed in base is what the worker fills;
  they never write the statement themselves. Once committed,
  `statement-immutability` catches a worker who touches it —
  **blocking on lean4 and isabelle, reporting only on rocq**.
  Publishing isn't gated on the placeholder existing: forget to commit
  it, and the check has nothing to compare against and stays silent —
  committing before publishing stays your discipline, not the gate's.
- **Write comments in final form.** A docstring says what the declaration
  says, not how you arrived at it. Rejected routes, rewritten statements,
  and the reason for a split go in the plan's log. Hold workers to the
  same line in review.
- **Shared definitions are authored at the centralized layer.** A
  definition other tasks' signatures depend on is an *interface*: write
  it yourself — commit under `auto`, draft for the overseer under
  `approve`/`manual` — then publish `prove` tasks that reference it. A
  definition used only inside one proof can ride along in a worker task.
  The test: *does anything else depend on this definition's exact
  shape?* Yes → centralize it. No → it's distributable. **Axioms move
  only by human decision; shared definitions move only at the
  centralized layer** — same logic, one notch softer.
  `statement-immutability` compares the *whole* declaration,
  token-identical, for kinds whose body is content — a `structure`'s
  fields, an `inductive`'s constructors, a `class`/`axiom`, a `def`'s
  computed value — except a placeholder body a worker may fill; blocking
  on lean4 and isabelle, reporting only on rocq. It covers only an
  existing base declaration in a touched file; a newly added definition
  that shadows a shared one isn't caught — that's stage-two review's
  job, and the indexer's once it ships.
- **Rewriting a shared definition is a base-SHA change.** Re-pin and
  republish dependent unclaimed tasks; `choir orch lease <n>` says which
  are in flight, and for those comment on the issue so the worker can
  rebase or release. Record the rewrite
  and why in the roadmap's log.
- New axioms are **never yours to grant**. If a task seems to need
  one, stop and ask the overseer; once granted, the overseer updates
  `.choir/verify.toml` on the protected branch and you record the
  rationale in the roadmap's log.

- **Publish a node whose proof route you don't know.** State the frontier you
  can state and let contributors decompose the interior. Label it `hard` or
  leave it unrated, never `easy`, and say in the body: *route unknown; a
  reduction is an acceptable return.*
- **Always write the library-and-project facts. Write the route only if you
  already have it.** Which library routes are unsupported, which project
  lemmas exist, what shape the statement is — these cost a few greps, and a
  worker discovers them late. Don't derive a route in order to write it down.
- **Ratify locally.** A split needs the node's statement and the intended
  argument's shape, not how a child will be proved; a child's split needs that
  child and its group's prose. Read wider when the decision is
  interface-shaped: a shared definition, a convention that must match across
  groups, a statement other nodes will be written against.

## Priority and difficulty labels

**You are the sole writer of `choir/priority:*` and
`choir/difficulty:*`** (`orchestrator/labels.py`: `set_priority`,
`set_difficulty`, `bump_priority`).

- Estimate difficulty at publish (easy/medium/hard, or unrated) and
  revise from evidence. The `struggle` signal at ≥2 failed attempts
  means at least `hard`; an *easy* task failing repeatedly means it's
  mis-stated, not hard — re-examine the statement instead.
- Each loop, bump aging unclaimed tasks one tier (`bump_priority`).
- Priority expresses urgency to workers, who claim priority-then-age.
  Publish-timing is reserved for readiness, early-run calibration, and
  imminent-replan holds (`ORCHESTRATOR.md` § Update the plan and
  publish).

## Stuck tasks

Deciding what to do about a stuck task is yours — Choir hard-codes no
escalation. Decide from data, not memory.

**The signal.** Each loop, read the per-task attempt history:

```bash
choir orch metrics struggle <repo> --min-failed 1
```

Per open task it reports PRs opened, PRs closed unmerged (`failed`),
distinct contributors, and time since publish — recomputed from
GitHub each time, so it survives restarts. PRs link to their task by
branch name (`choir/<n>-…`); your own pushes to the protected branch
don't count.

Two modes are quieter than a red PR: a task **claimed long ago with no
PR** (the worker gave up without committing; the reconcile window
frees the claim eventually — treat it as a failed attempt), and an
**abandoned red PR** (a worker submits and moves on,
so a red PR you commented on may never be revised — count it as a
failed attempt too).

**The threshold is a heuristic you apply with judgment, not a rule the
gate enforces.** Default: act at **≥2 failed attempts**, or published
longer than the reconcile window (`[reconcile].stale_after_days` in
`.choir/project.toml`, default 7) with no green PR. Loosen it for a
theorem you expected to be hard; tighten it for one that should be
trivial.

When a task crosses the line, act in roughly this order:

1. **Diagnose a shared cause.** Several attempts failing the same way
   is one fixable gap, not N hard tasks — write it to
   `skills/orchestrator-notes.md` (next section) so the fix reaches
   every future worker.
2. **Decompose.** State the intermediate lemmas yourself, commit them
   as `sorry`-bearing declarations, and publish each as its own
   `prove` task (the centralized-layer rule above). **Decompose
   recursively and on your own**: if a sub-task also struggles, split
   it further. The `struggle` signal is your trigger — don't wait for
   the overseer to notice, and don't ask permission to break it down.
3. **Park it as a stated-but-unproved node.** Under `sorry = report`, commit the
   best partial (or just the sorried statement), drop its priority,
   and move on. Not every node must close now.
4. **Escalate.** A new axiom, an uncertain shared definition, or a
   wrong-looking statement is an overseer decision — surface it,
   don't paper over it. Even then, keep working everything else.

Record the decision — which task, why, what you did — in the
roadmap's log.
The `struggle` numbers recompute each loop; your reasoning does not.

## Golf tasks — annealing the merged corpus

A merged proof is honest but not necessarily good. `type: golf`
rewrites one existing proof to be shorter, faster to elaborate, and
clearer, with the statement untouched.

Golf is the task type the gate verifies best: the target declaration
already exists in base, so a real blocking statement check binds —
`verify-comparator` on lean4 (v4.27+), `statement-equiv` on isabelle
and rocq. `statement-immutability` also covers it: blocking on lean4
and isabelle, reporting only on rocq.

**You pick the targets; workers never self-select** — leases are per
issue, so two workers must not land on one proof. **Pick them by length:**
rank the project's declarations by proof lines and publish the top few.
Skip files where in-flight tasks are working.

Do not read the proofs to find the improvement. Whether a length is
intrinsic to the mathematics, and what to do about it if not, is the
worker's call on the worker's compute — a worker who finds nothing worth
doing says so in a comment and you close the task.

**When to publish.** After an area stabilizes, not mid-construction.
Label `choir/priority:low` — golf never competes with prove tasks —
and usually `choir/difficulty:easy`.

**Writing the prose.** Title `golf: <target_decl>`; the task body:

    Golf the proof of `<target_decl>` in `<target_file>` (currently
    <N> lines).

    Rules:
    - Do NOT change the statement — name, namespace, signature, and
      the stated proposition stay exactly as they are. Only the proof
      body changes.
    - Change exactly one proof. Leave every other declaration,
      import, and docstring untouched.
    - Improve along three axes: length (fewer lines, less
      repetition), elaboration speed (prefer targeted lemmas over
      heavy automation), and structure (clearer, closer to the
      project's conventions). A shorter proof that compiles slower is
      a regression, not a golf.
    - If the length is intrinsic and there is nothing to win, say so
      in a comment rather than forcing a rewrite.

Add anything you know that a worker reading one file cannot — that a
helper here duplicates a library lemma the project already uses
elsewhere, say. One sentence. Finding and making the change is the
worker's job, not yours.

**Reviewing a golf PR.** Concentrate on what the gate can't see:
elaboration-time or readability regressions, and axiom-closure drift
within the permitted set. Salvage works unchanged: `rebuild` or
`decide-instance` failures are a `NEAR_MISS` follow-up; trust failures
reject as always. `style` is advisory, so a golf attempt that didn't
shorten anything comes back green.

## Recording what you learn — `skills/orchestrator-notes.md`

You see every PR, so you learn the project's recurring failure
modes — a tactic workers keep misusing, a definition shape that keeps
producing vacuous theorems, a convention they keep missing. Write the
lesson down so the next worker starts knowing it; autosync delivers
the skill pack to every worker's next task.

**`skills/orchestrator-notes.md` is yours to maintain. Never edit the
overseer's hand-authored skill files** — everything else in `skills/` is
theirs, and provenance is by filename, so anything outside that file is
human-authored by construction.

Every file in `skills/` is force-loaded into a worker's context at claim
time, so this one holds only what a worker can act on. A lesson about
running the loop goes in your own log (`ORCHESTRATOR.md` § Your working
memory), which no worker pays for — and a step every run needs is not a
lesson at all: put it in the loop.

`SKILL_PACKS.md` has the rest: what else a pack holds, and how it reaches
a worker's `.choir-context.md` at claim time.

Compress it every loop. An entry either still guides future work or it goes:

- a failure mode since fixed → delete
- guidance that has landed in the roadmap prose or a skill file → delete
- a note on attacking a node that is now proved → delete

Three things are maintained, not appended: this file, `roadmap/<group>.md`, and
the prose of every published task. Correct a published task's prose in place
when a fact in it stops being true — a lemma renamed, a library gap closed, a
route retracted. A correction on the PR does not reach the next claimant; they
read the issue. The overseer
can see what you added (`git log -- skills/orchestrator-notes.md`) and
edit, delete, or promote any entry. Date each entry, link the task or
PR that prompted it, and keep it tight — active guidance, not a
changelog.

Commit under `auto`; under `approve`/`manual`, draft the entry and
surface it in your overseer report for a human to commit. A new note
reaches the next claim, never an active proof.
