"""Choir orchestrator toolkit — primitives for the orchestrator agent.

The orchestrator is an LLM agent running on the *overseer's* machine,
billed to the overseer's own LLM account. It plans the project,
decomposes goals into tasks, publishes them as GitHub issues, monitors
incoming PRs, reviews the mathematics, decides merges (subject to the
gate's deterministic floor and the project's automation level), and
replans as results land. See `docs/agents/ORCHESTRATOR.md` for the playbook.

This package is that agent's toolkit. It is **agent-agnostic by
design**: plain Python wrapping the `gh` CLI, with no LLM-vendor
dependencies of any kind — no model SDKs, no API calls, no keys. Any
agent that can run a shell command (or import Python) can drive Choir;
overseers can switch agents mid-project without touching anything here.

Submodules:

- `orchestrator.tasks`   — create / list / read Choir task issues
- `orchestrator.prs`     — list / inspect / comment / merge pull requests
- `orchestrator.metrics` — per-task lifecycle metrics (JSON/CSV CLI)
- `orchestrator.project_config` — `.choir/project.toml` (automation level)

Authentication is the overseer's local `gh auth login`. Choir never
sees or forwards anyone's credentials — LLM or GitHub.
"""
