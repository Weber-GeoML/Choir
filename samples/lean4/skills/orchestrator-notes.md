<!-- choir:orchestrator-notes — machine-authored, overseer-curatable -->

# Orchestrator notes

This file is maintained by the **orchestrator** (the project's LLM agent),
not hand-authored by the overseer. It records failure modes and operational
guidance the orchestrator learns while reviewing PRs during a run, so the
next worker starts knowing them. It is delivered to every worker's next task
via skill autosync (design note 06) and carries the same weight as the rest
of the skill pack.

**Provenance.** The orchestrator writes only this file; every other file in
`skills/` is overseer-authored. The overseer may edit, delete, or promote any
entry here freely — `git log -- skills/orchestrator-notes.md` is the full
record of what the orchestrator added.

**Audience.** Workers. The name says who writes this file, not who reads it:
anything only the orchestrator acts on belongs in its own log under `roadmap/`,
which no worker loads.

**Format.** Newest first; each entry dated, with the task or PR that prompted
it. Keep it tight — active guidance, not a changelog; prune resolved entries.

---

_No entries yet._
