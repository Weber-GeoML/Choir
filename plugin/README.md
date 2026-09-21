# Choir (Claude Code / Codex plugin)

Community-led formalization in kernel-checked proof assistants — Lean 4,
Isabelle, and Rocq. This plugin gives your agent two entry points into
[Choir](https://github.com/Weber-GeoML/Choir): start a project as its
**overseer**, or join one as a **contributor**.

## Install

**Claude Code**

```text
/plugin marketplace add Weber-GeoML/Choir
/plugin install choir@choir
```

**Codex** (from your shell)

```text
codex plugin marketplace add Weber-GeoML/Choir
codex plugin add choir@choir
```

The shell is the only way to register a third-party marketplace. Codex's
in-session `/plugins` browser searches, installs and toggles plugins from
marketplaces that are already registered, so run the first command above
before looking for Choir there.

## What it does

Two skills, matched from ordinary prose — you don't need to remember a command
name — plus one command for when you do:

- **`formalize`** — "formalize this paper in Lean 4", "resume my Choir
  project". Sets you up as the overseer: your agent becomes the project's
  orchestrator, which plans the work, publishes tasks as GitHub issues, reviews
  incoming proofs, and merges what passes the gate.
- **`join`** — "join this Choir project", "contribute to
  `github.com/someone/their-project`". Sets this machine up as a contributor:
  your agent claims tasks, proves them locally, and opens PRs.
- **`/choir:purge`** — removes this machine's local state for a project, or
  all of it. Your checkout, your fork, and the project on GitHub are
  untouched.

Codex users: this plugin does not bundle `/choir:purge` as a Codex command —
run `choir purge` directly instead.

## What it needs

- [`gh`](https://cli.github.com), installed and authenticated (`gh auth login`).
- The prover toolchain for the project you're working on — for Lean 4 the
  setup script installs `elan` if it's missing.
- Python 3.11+ and [`uv`](https://docs.astral.sh/uv/).

Your own LLM account is what pays for the agent work, on your own hardware.
Choir never sees, stores, or forwards LLM credentials — that's a design
non-negotiable, not a configuration choice.

## How the plugin relates to Choir

The skills define no behaviour of their own. Each one clones (or refreshes) a
Choir checkout, runs the setup script, and then defers to the playbook —
`docs/agents/ORCHESTRATOR.md` for overseers, `docs/agents/CONTRIBUTOR.md` for contributors.
The playbooks are the single source of truth, which is what keeps Choir
agent-agnostic: the same scripts and playbooks work from any agent, or by hand.

## Links

- Repository, issues, and full documentation:
  <https://github.com/Weber-GeoML/Choir>
- Knobs, labels, and what each gate check covers:
  [`docs/reference.md`](https://github.com/Weber-GeoML/Choir/blob/main/docs/reference.md)
- License: Apache-2.0
