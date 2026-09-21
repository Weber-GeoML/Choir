---
name: formalize
description: Start or resume a Choir formalization project as its overseer — formalize a theorem, paper, textbook chapter, or folder of sources in Lean 4, Isabelle, or Rocq by orchestrating AI contributor agents behind a deterministic verification gate. Use when the user wants to formalize something with Choir or run/resume a Choir project they oversee.
argument-hint: "[goal, sources, or owner/repo]"
---

# Formalize with Choir (overseer)

Choir is an open protocol for community-led formalization in kernel-checked
proof assistants: contributor agents prove tasks on their own machines and
LLM accounts, and a deterministic gate verifies every submission before
merge. You are about to act as the project's orchestrator on the overseer's
behalf: plan, publish tasks, review contributions, merge per the automation
level.

This skill is a pointer. The behavioral contract lives in Choir's playbook —
never improvise a step the playbook or its scripts already own.

1. **Understand the goal.** Take it from the arguments and the
   conversation: a theorem, a paper or textbook chapter, a folder of
   sources, or an existing Choir project repo to resume. Treat the current
   folder as the project's sources unless told otherwise.

2. **Ensure a Choir checkout at `~/.choir/checkout`.** If missing:

       gh repo clone Weber-GeoML/Choir ~/.choir/checkout

   If `gh` is missing or unauthenticated, have the user install it and run
   `gh auth login` first.

3. **Machine setup.** When resuming a project whose repo you already know:

       sh ~/.choir/checkout/scripts/orchestrator-init.sh <owner/repo>

   For a fresh project, the playbook establishes the repo: follow
   `~/.choir/checkout/docs/agents/orchestrator-setup.md`'s bootstrapping
   procedure (it drives `scripts/new-project.sh` and the configuration
   interview), then run `orchestrator-init.sh` with the new `owner/repo`.

4. **Read `~/.choir/checkout/docs/agents/orchestrator-setup.md` and
   `ORCHESTRATOR.md` and drive the project by them,** starting with the
   configuration interview. Bootstrap or resume as the playbook
   determines, then run the plan/publish/review/merge loop in this
   session at the project's configured automation level.
