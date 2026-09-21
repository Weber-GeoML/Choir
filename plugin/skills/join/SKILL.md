---
name: join
description: Join a Choir formalization project as a contributor — set up this machine and start proving tasks (Lean 4, Isabelle, or Rocq) with your own agent on your own LLM account. Use when the user wants to join, contribute to, or work on a Choir project or its GitHub repo.
argument-hint: "[owner/repo]"
---

# Join a Choir project as a contributor

Choir is an open protocol for community-led formalization in kernel-checked
proof assistants: tasks live as GitHub issues, submissions are PRs, and a
deterministic gate verifies every submission before merge. You are setting
this machine up as a contributor — the user's agent, hardware, and LLM
account.

This skill is a pointer. All real behavior lives in Choir's scripts and
playbooks — never improvise a step the script or playbook already owns.

1. **Resolve the target project.** Accept `owner/repo` or a full GitHub URL
   (reduce it to `owner/repo`). If no project was given, ask the user which
   project to join — never guess.

2. **Ensure a Choir checkout at `./choir`** (relative to the current
   directory). If missing:

       gh repo clone Weber-GeoML/Choir ./choir

   If `gh` is missing or unauthenticated, have the user install it and run
   `gh auth login` first.

3. **Run the setup script, and re-run it until it reports READY:**

       sh ./choir/scripts/join.sh <owner/repo>

   The script is idempotent and owns every prerequisite check and install.
   Fix what its `ACTION NEEDED` lines flag, then re-run. Do not hand-install
   or work around anything the script manages.

4. **Read `./choir/docs/agents/CONTRIBUTOR.md` and operate by it from here on.**
   That document is the contributor's operating manual: claiming a task,
   proving it, submitting, and what the gate will check. Everything after
   setup is its domain, not this skill's.
