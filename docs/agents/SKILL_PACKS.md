# Skill packs — per-project knowledge transfer

A *skill pack* is a directory of Markdown files in a Choir-managed
project repo, under `skills/`, that the orchestrator distributes to
every contributor agent via the standard workspace clone. It's how
project-specific knowledge — conventions, blueprint pointers,
dependency rules, examples — reaches the agent in a consistent form
regardless of which agent the contributor is running.

## The four layers an agent reads in a Choir workspace

| Layer | Source | Lifetime | Built? |
|---|---|---|---|
| **Prover substrate** | the contributor's own prover tooling — e.g. `lean4-skills` on lean4 | One install per contributor | external |
| **Task** | `TASK.md` (issue prose, dropped by Choir) | One per task | ✓ |
| **Project** | `skills/` in the project repo (authored there by the maintainer; synced to latest at task start into `.choir-skills/`, assembled as `.choir-context.md`) | Updated per-project; synced to default-branch tip at task start (not pinned to task commit) | ✓ (mechanism); example shipped in `samples/lean4/skills/` |
| **Choir-protocol** | `CHOIR.md` (dropped by `client/workspace.py`) | Constant — same for every workspace | ✓ |

All four sit in the workspace filesystem, so an agent working there
reads whichever it needs.

## File layout

`skills/` is just a directory of Markdown. **There's no schema** —
the agent reads the whole directory; structure it however helps
comprehension.

Suggested filenames Choir's own tooling looks for (or that future
audits might consume), all optional:

- `PROJECT.md` — overview of the project, scope, mathematical theme,
  pinned dependencies. Strongly recommended.
- `conventions.md` — naming, formatting, namespace structure.
- `tactics.md` — preferred proof tactics, common patterns for this
  project.
- `blueprint.md` — pointer to the blueprint (LaTeX, Markdown,
  wherever), if applicable.
- `dependencies.md` — what Mathlib / external libraries are pinned,
  what to avoid.
- `examples.md` — worked examples of closing sorries in this project.

One filename is **reserved and machine-authored**: `orchestrator-notes.md`
is the orchestrator's append-only channel for failure modes it learns
during a run. The orchestrator writes only this file; the overseer
authors all the others and may edit, delete, or promote any of its
entries. To a worker it reads as ordinary guidance, force-loaded with the
rest.

**It is the orchestrator's channel *to workers*, not its own notebook** —
the name says who writes it, not who reads it. Anything only the
orchestrator acts on (how to run the loop, what to check before merging)
belongs in its own log under `roadmap/`, which no worker loads and so no
worker pays for. Provenance is therefore by filename —
`git log -- skills/orchestrator-notes.md` is the record of what the machine
added.

For a minimal demonstration, see `samples/lean4/skills/` — `PROJECT.md` +
`conventions.md`, plus a seeded `orchestrator-notes.md` header showing the
reserved file's format.

## Mechanism

When `choir worker claim` runs:

1. Clone the project repo at the pinned commit from `project_ref.commit`.
   The code tree (lean files, lakefile, Mathlib pin) is frozen at that
   commit for reproducible verification.
2. `setup_workspace` fetches the **latest** `skills/` from the project's
   default-branch tip and extracts it into `<ws>/.choir-skills/` — an
   out-of-tree location registered in `<ws>/.git/info/exclude` so it
   can never be committed. This decouples skill freshness from the code
   pin: the agent sees today's guidance even if the task was opened
   against an older commit.
3. `setup_workspace` assembles `<ws>/.choir-context.md` from every file
   in `.choir-skills/`, under a header naming it the maintainer's current
   conventions. This is the **single authoritative guidance file** the
   agent reads; `CHOIR.md` and `TASK.md` sit beside it, not inside it.
4. `setup_workspace` drops `TASK.md` and `CHOIR.md` alongside.
5. `CONTRIBUTOR.md` tells the agent to read `.choir-context.md` first,
   before `CHOIR.md` and `TASK.md`.

The maintainer authors skills by committing `skills/` to the default
branch as usual; the propagation to running agents is automatic — no
manual re-pull step needed.

## Versioning

Skill packs sync to the **latest default-branch version** at task start,
not the task's pinned code commit. The assembled guidance lands in
`.choir-context.md` in the workspace root — the agent reads that file
rather than `skills/` directly, ensuring it always sees the maintainer's
current instructions.

A task already in progress keeps the `.choir-context.md` it began with;
Choir does not re-sync mid-flight. The code tree itself (lakefile, lean
files, Mathlib pin) remains pinned to `project_ref.commit` from the
TaskRecord — only the skill guidance updates to tip.

## What's NOT in scope

- Choir does not enforce that a skill pack exists. A project with no
  `skills/` works fine; the agent just has less context.
- Choir does not validate skill-pack contents. It's just Markdown;
  the agent reads whatever's there.
- Choir does not check the contributor's prover tooling. The
  contributor is responsible for keeping that updated. The project pins
  its own toolchain — `lean-toolchain` on lean4, `project_ref.toolchain`
  on isabelle and rocq.

## How agents discover the layers

The `CHOIR.md` primer names each one and points at `.choir-context.md` as
the authoritative, current guidance. `samples/lean4/skills/` is kept short
so the assembled file stays small.
