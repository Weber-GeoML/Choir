# Orchestrator — setup

Read this once per project: prerequisites, search tooling, permissions,
local config, bootstrapping, upgrading a mid-progress project, onboarding
contributors, external acceptance checks, and a worked example. The loop
itself is in `ORCHESTRATOR.md`.

## Prerequisites

Run this first, and fix anything it flags:

```bash
sh ~/.choir/checkout/scripts/orchestrator-init.sh <owner/project-repo>
```

It checks `gh` auth and push rights, installs `uv` if missing, syncs the
Choir checkout, and puts `choir` on your PATH — so every command below
runs from wherever you are. If it flags that `~/.local/bin` is not on
your PATH, fix that before going on; nothing else in this playbook works
until `choir` resolves.

The checkout lives at `~/.choir/checkout` — one durable checkout per
machine, shared by every project you orchestrate. Per-project state
lives under `~/.choir/projects/<owner>/<repo>/`, never in the
checkout. The checkout is code, not cache: don't delete it to reclaim
space. **Never point a worker at it** — workers use disposable
per-session `./choir` clones, so a contributor updating their checkout
never swaps code under a running orchestrator.

You'll also want a local clone of the project repo, for inventory
scans and policy edits, at the path in your local config (below).

### Search tooling

On a Lean project, install a Mathlib search tool before you plan anything.
You need it for two jobs a contributor never does: **decomposition**, where
publishing a task for something Mathlib already proves wastes a
contributor's whole claim, and **review**, where judging whether a proof
reached for the right lemma means being able to look the lemma up yourself.

The setup is the same one contributors follow —
`contributor-setup.md` → "Mathlib search tooling" has it per harness:

```bash
claude mcp add lean-lsp uvx lean-lsp-mcp      # Claude Code
codex mcp add lean-lsp -- uvx lean-lsp-mcp    # Codex
```

### Permissions

An orchestrator that stops at an approval prompt every loop is not running
unattended, and one granted `python -c` is granted everything. The
commands exist so that whatever your harness offers has something specific
to name — and what it offers varies, so this is guidance rather than a
configuration to paste.

**Harnesses with a command allowlist** (Claude Code) can grant the toolkit
and nothing else. In `~/.claude/settings.json` — yours, not the project
repo, which would ship it to contributors who have no use for it:

```json
{
  "permissions": {
    "allow": ["Bash(choir *)"],
    "deny":  [
      "Bash(choir orch * merge-override*)",
      "Bash(choir orch merge-override*)",
      "Bash(uv run choir orch * merge-override*)",
      "Bash(gh pr merge*)",
      "Bash(gh api * /merge*)"
    ]
  }
}
```

`merge-override` is denied rather than simply left out, because `allow`
here is a prefix and would otherwise cover it — that is exactly why the
override is a separate subcommand and not a flag on `merge`. The other two
deny entries close the paths that reach a merge without the gate's
preflight at all.

**Two entries, because the subcommand is not the token after `orch`.**
`--repo` is an option on `orch` itself, so the invocation this playbook
writes everywhere is `choir orch --repo org/project merge-override 12
--reason "…"` — a rule anchored at `choir orch merge-override` never fires
against it, while `allow` matches it in full. The first entry covers that
order and the second the form with `--repo` defaulted. **Confirm both
match on your harness's matcher before relying on either.**

**And a string rule binds a spelling, not a capability.** `choir` is the
one setup installs, but `uv run choir …` from the checkout reaches the
same code, and so does `python -m orchestrator.cli`. Deny the spellings
you can, and read the result as a guardrail against an agent taking the
convenient path, never as a boundary. The boundary is that
`merge-override` is the overseer's to run, and the orchestrator is told so
directly (`ORCHESTRATOR.md` § Automation levels).

**Harnesses with sandboxing and approvals** (Codex: `--sandbox
read-only | workspace-write | danger-full-access`, plus its approval
policy) cannot express that split — there is no per-command allowlist to
put `merge` in. What the commands buy you there is legibility: an approval
prompt reading `choir orch merge-override --reason "…"` is recognisably
different from `choir orch merge`, where two `python -c` blobs are not.
Run the orchestrator with approvals on for anything outside its workspace,
and read what you are approving.

**Every harness** gets two things regardless. A named command appears in
the session transcript, so what the orchestrator actually did is greppable
after the fact. And the agent does not have to write correct Python before
it can read an answer, which is where a whole class of error comes from.

Treat all of this as a guardrail, not a boundary. An agent with a shell
and write access can edit these settings. What it buys is that the
orchestrator does not casually route around the toolkit — the failure that
actually happens. The gate itself is enforced on GitHub, by the workflows,
not here.

## Your local config

Set this up before anything else. It's per-project — one machine can
run several Choir projects — so the file is namespaced by repo rather
than shared. (A legacy single `~/.choir/orchestrator.toml` is still
read as a fallback.)

**Interview the overseer — don't guess, and never use a default
without telling them.** Ask:

1. How often should you check GitHub for changes? (default: 10
   minutes)
2. Where should the local project checkout live? (default: `~/` + the
   repo name)

Write the answers to `~/.choir/projects/<owner>/<repo>/orchestrator.toml`:

```toml
# Local to this machine — never commit, never copy into the project repo.
[project]
repo = "<owner/project-repo>"
checkout = "~/<project>"

[loop]
poll_interval_seconds = 600
max_wait_seconds = 3600
```

**Never commit or push this file, and never quote its contents into
the roadmap, issues, or PRs.** It lives under the home directory,
outside every repo, and stays there.

Repo-side `.choir/project.toml` and `.choir/verify.toml` are different
— shared project policy, and they belong in the repo.

On resume, read the config instead of re-interviewing; re-ask only if
the overseer wants to change something.

## Your session brief

Setup put one line in the `AGENTS.md` of the folder you were launched in,
pointing at the playbook. **Write your brief above that line, in the same
file, as soon as the interview is done.**

That file and the `CLAUDE.md` beside it are the only things a new session
reloads on its own. Your context window does not survive a restart and a
compaction keeps a summary, not the prompt — so a fact the overseer told
you once and you did not write down is a fact the next session will ask
for again, or guess.

Write what a competent stranger would need before touching anything:

- the project repo, as `<owner/name>`, and the local checkout path
- the goal, in the overseer's own words
- any standing rule or preference they gave that the playbook does not
  already cover — a convention to follow, a file to leave alone, a source
  to formalize from

Judgement is yours: keep what still governs the work, drop what was
scaffolding. In particular **never record the command or script that
launched you.** Setup has already run, and a session that re-runs it is
redoing finished work instead of the loop.

Keep it to something readable at a glance — it is reloaded every session,
and a brief nobody reads costs more than it saves. The roadmap in the
repo, not this file, is where the plan and the reasoning live
(`ORCHESTRATOR.md` § Your working memory). Revise the brief when one of
its facts stops being true.

```markdown
# Choir orchestrator — <owner/name>

Formalizing <source> in <prover>. Repo `<owner/name>`, checkout at
`<path>`. The overseer wants <the goal, their words>.

Standing rules: <anything they asked for that the playbook does not say>.

Work is already under way — read the roadmap and the open tasks and
continue; do not re-bootstrap or re-plan from scratch.

Read the playbook `~/.choir/checkout/docs/agents/ORCHESTRATOR.md` and act
as the orchestrator.
```

## Bootstrapping or resuming a project

When the overseer gives you a goal, check whether the project repo
exists (`gh repo view <owner/name>`).

**If it exists, resume — never re-bootstrap.** Read the roadmap and the
reasoning notes, the open issues (`choir orch tasks --state open`),
and the open PRs (`choir orch prs`); reconstruct where things stand and continue the loop.
If the repo doesn't tell you enough to resume cold, that's a defect in
how the working memory was kept — fix it first.

**If it doesn't exist, bootstrap.** The mechanical part is scripted:

```bash
~/.choir/checkout/scripts/new-project.sh <owner/name> [--from <git-url>] [--toolchain <pin>] [--public] [--prover lean4|isabelle|rocq]
```

This gives you the project base, the Choir overlay (packages,
workflows, issue template), default `.choir/` policy (automation
`auto`, axiom policy `net_zero`), labels, and a branch-protection
attempt. `--prover` defaults to `lean4`; the isabelle/rocq templates
are UNVALIDATED, so review the generated `verify-pr.yml` before
trusting it on a real project.

**Never assemble the overlay by hand, and never copy it via
`cp -r <choir>/.github/workflows`.** That directory is Choir's own
self-CI, not a project's gate, and two required workflows
(`verify-pr.yml`, `verify-trust-report.yml`) have no template file —
the script generates them per prover. A partial copy yields the worst
possible repo: one whose PRs look green because the audits never ran.
`gate/checks.py` is the authority on which checks must be present. If
you must bootstrap manually, read `scripts/new-project.sh` and mirror
it exactly.

**Confirm branch protection took** — the script attempts it and
cannot succeed on a free private repo. `merge_pr`'s preflight enforces
the floor either way (`ORCHESTRATOR.md` § Your toolkit).

The project-specific part is yours:

- **Toolchain**: the overseer fixes the version at creation; a
  `--from` project keeps its own pin instead — the `lean-toolchain`
  file for lean4, or the `project_ref.toolchain` field you stamp on
  every task for isabelle/rocq, since those provers have no
  repo-committed toolchain file. Never bump it mid-run.

  On lean4, say which versions keep the kernel statement gate working
  before the overseer picks. `verify-comparator` builds the comparator at
  the tag matching the pin, and comparator tags only `.0` releases — so
  Lean's newest patch release leaves that gate unable to run on every PR,
  and the pin is not something you can change later. `new-project.sh`
  defaults to the newest release comparator has and prints what it chose;
  an explicit `--toolchain` it cannot support gets a warning, not a
  refusal — the pin is the overseer's call, so make sure they are making
  it knowingly.
- **Policy**: interview the overseer, then edit `.choir/verify.toml`
  and `.choir/project.toml` on the protected branch before publishing
  tasks — automation level (`auto`/`approve`/`manual`), axiom policy
  (`net_zero`, or a whitelist), and sorry policy (`block`, default, or
  `report` for partial-progress merges). `docs/reference.md` documents
  every knob, label and check in one place. Prefer `block` even when
  partial progress is expected: a reduction lands it with its
  obligations named (`orchestrator-review.md` § A reduction), so
  `report` is only for a project that wants unnamed placeholders to
  merge.
- **The source**: when the project formalizes something the overseer
  did not write — a paper, a textbook, lecture notes — ask how it
  should reach contributors. The right to redistribute it may not be
  theirs to give, and the answer decides what the roadmap holds, so
  settle it before writing one:

  1. commit the source to the repo;
  2. link to it and commit nothing;
  3. transcribe what the tasks need, in your own words, and commit
     that.

  Ask even when the answer looks obvious, and record it in
  `roadmap/README.md` so a later session does not decide it again.
- **Build**: review `.github/workflows/verify-pr.yml`; adapt it if the
  project needs more than the general template.
- **Branch protection**: if the script's attempt failed, tell the
  overseer and agree how to proceed.
- **Skill pack**: write `skills/` if contributors need project
  conventions (`SKILL_PACKS.md` for the layout and how it reaches a
  worker; `samples/lean4/skills/` for a minimal one).
- Then write the roadmap (`orchestrator-planning.md` § The roadmap),
  publish the first tasks, and print the joining prompt (below) for the
  overseer to hand to contributors.

## Upgrading a mid-progress project

`PROTOCOL_VERSION` (`gate/protocol.py`) bumps whenever a change alters
what parties must agree on: task schema, label vocabulary, `.choir/*`
config shapes, lease/branch conventions. A project's overlay is a
bootstrap-time snapshot and can drift behind the checkout driving it.

**Check for drift at resume or loop start, never mid-loop** — a
running session can't hot-swap its own code or its already-read
playbook:

- Is your Choir checkout behind its `origin`?
- Does the project's `.choir/project.toml` `choir_protocol` pin differ
  from what your checkout would write?

**A stale overlay or pin makes every PR refuse to merge at once, for
no reason visible in any diff.** `merge_pr`'s preflight requires every
check in `gate.checks.REQUIRED_PRESENT` — `comparator` and
`statement-immutability` among them — to be present. A check the
project's overlay predates never ran at all, and the preflight treats
absent exactly like failing: every open and future PR refuses to
merge, regardless of what each PR changed, until the repo is upgraded.
This is fail-safe, and self-healing: run `upgrade-project.sh`.

**Automation-level behavior:**

- **AUTO**: `git pull --ff-only` the checkout and re-read this
  playbook when it's behind origin — pull only at a loop boundary or
  when idle if another session on this shared checkout is mid-loop,
  never under it. Run `scripts/upgrade-project.sh <local-clone>
  --repo <owner/name>` whenever the overlay or pin differs. No
  permission-asking — this is routine maintenance.
- **APPROVE / MANUAL**: surface the drift and wait for the overseer
  to act.

**Never hand the overseer a file-edit instruction — their interface
is conversation and Markdown files, never code, config, or JSON.**
This includes the wire-fingerprint guard
(`gate/protocol_fingerprint.py`): if it fires, decide compatibility
yourself, or ask the overseer conversationally under genuine
ambiguity, then run `uv run python -m gate.protocol_fingerprint
--update` or bump `PROTOCOL_VERSION` yourself. Never tell the
overseer to edit `gate/protocol_fingerprint.json` directly.

**Invariant: your checkout's version is always ≥ the repo's pin.** If
you find a repo pinned ahead, another machine already upgraded it and
yours is stale — `git pull --ff-only` (and reinstall if needed) before
doing anything else. Never run `upgrade-project.sh` backwards, and
never interpret a wire version you don't yet speak.

**Record every successful upgrade in the roadmap's log**: date, old→new
protocol version, and the Choir commit the overlay now matches.

**What `upgrade-project.sh` does and refuses.** It aborts on a dirty
working tree or a non-default branch. Otherwise it syncs the
`gate`/`orchestrator`/`client` packages, `pyproject.toml`/`uv.lock`,
the issue template, and the verify workflows; re-runs
`setup-labels.sh`; and writes the `choir_protocol`/`choir_commit`
pin — one commit, pushed unless `--no-push`. **`verify-pr.yml` is
protected by default**: overwritten only with `--force-build-workflow`.
It never touches open PRs, `choir/*` branches, `roadmap/`, skills, or
project sources, and a
repeat run against an already-current project is a no-op.

In-flight PRs validate against the base SHA they opened against,
**except** `missing_required_checks`: it reads your checkout's own
`REQUIRED_PRESENT`, not the repo's, so once your checkout gains a new
required check, an open PR whose rollup predates that check's workflow
is refused at merge until a push or reopen re-triggers CI. Upgrading
raises only the *repo's* pin — pre-upgrade workers keep claiming with
old code until they run `choir update`, so nudge contributors toward
it when you upgrade a project they're actively working on.

## Onboarding contributors — the joining prompt

**The joining prompt is hardcoded — never compose it yourself.** After
bootstrap, and whenever asked, generate it and hand it to the overseer
verbatim:

```bash
choir orch joining-prompt <owner/project-repo>
```

It points the contributor's agent at `scripts/join.sh` (prerequisite
checks/installs and Choir sync) and
`CONTRIBUTOR.md` (the behavioral contract). Pass `--choir-url` if
contributors should clone a fork of Choir.

## External acceptance checks

Some projects use a scoring or validation tool beyond the gate
(benchmark comparators, proof checkers, style gates). Those stay
outside Choir core. Run them at the point the overseer specifies —
typically post-merge — and record results in the roadmap's log.
If a merged PR fails the external check, treat it like any other
defect: open a follow-up task or revert, and tell the overseer.

**Reading `verify-trust-report`.** This informational check asks the
prover's own kernel what a declaration actually depends on (`#print
axioms` for lean4, `Print Assumptions` for rocq, kernel
oracle-tracking for isabelle), rather than text-scanning for keywords.
It autodetects a PR's changed declarations, probes each one, and turns
anything it can't resolve into an `unresolved` line rather than
failing the run (capped at 50 declarations per run). For ad-hoc
queries — a decl not on this PR's diff, a dependency's assumptions, an
arbitrary checkout — invoke the CLI directly with explicit targets:
`uv run python -m gate.verify.trust_report_cli --workspace <checkout>
--decl <name> --import <module>`. It never fails the PR: read a
non-`clean` `TrustEntry` the way you'd read the trust inventory — it
names real assumptions, including ones smuggled in through a
dependency, which is stronger replanning signal than the text-based
axiom-honesty audit. It doesn't stop a merge by itself, but it's your
cue to look closer, and a repeated pattern is worth a note in
`orchestrator-notes.md`.

## Worked example: standing up a benchmark project

The overseer says: *"Run this 23-problem benchmark, one issue per
problem, axiom whitelist, full auto."* The whole setup is composed
from primitives — Choir has no benchmark mode:

```bash
# 1. Bootstrap, then add the problems (one .lean file each, statement +
#    placeholder body). Never hand-assemble the overlay:
~/.choir/checkout/scripts/new-project.sh org/bench --toolchain <pin>
# 2. Policy on the protected branch:
cat > .choir/verify.toml <<EOF
[audits.axiom_honesty]
policy = "whitelist"
allowed_axioms = ["propext", "Quot.sound", "Classical.choice"]
EOF
cat > .choir/project.toml <<EOF
[automation]
merge = "auto"
EOF
git add .choir && git commit -m "Choir policy" && git push
```

```bash
# 3. One issue per problem (your own loop, not Choir code):
for f in problems/*.lean; do
  decl=$(first_decl_name "$f")            # you write this
  choir orch --repo org/bench create-task \
      --title "prove $decl" \
      --target-file "$f" --target-decl "$decl" \
      --commit "$HEAD_SHA" --toolchain "$TOOLCHAIN" \
      --label benchmark/run-1
done
```

Then run the loop. Progress at any time:

```bash
choir orch metrics summarize org/bench --label benchmark/run-1 --format csv
choir orch inventory scan <bench-checkout> --format md   # sorries left = problems open
```
