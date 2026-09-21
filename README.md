<p align="center">
  <picture>
    <source srcset="assets/logo/choir-lockup-dark.svg" media="(prefers-color-scheme: dark)">
    <img src="assets/logo/choir-lockup.svg" alt="Choir" height="96">
  </picture>
</p>

<p align="center">An open protocol for distributed multi-agent autoformalization.</p>

<p align="center">
  <a href="https://github.com/Weber-GeoML/Choir/actions/workflows/ci.yml"><img src="https://github.com/Weber-GeoML/Choir/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="./LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-blue.svg" alt="License: Apache 2.0"></a>
  <img src="https://img.shields.io/badge/python-3.11%2B-blue.svg" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/status-pre--alpha-orange.svg" alt="Status: pre-alpha">
</p>

Choir aims to spread the cost of a large formalization. A human overseer runs an orchestrator agent on their own machine; Choir turns that agent's plan into tasks on a GitHub repo, where any contributor can claim one and work it with their own agent, billed to their own account. Every submitted pull request is audited by Choir's deterministic trust gate before it is reviewed and merged. Works with Lean 4, Isabelle and Rocq, and with whichever agent each participant already runs. The default planner and orchestrator are easy to swap for your own, or to slot into a workflow you already have.

See a demo repo: [ProbMethodCombinatorics](https://github.com/yidiq7/ProbMethodCombinatorics) is a Choir-powered autoformalization of Yufei Zhao's *Probabilistic Methods in Combinatorics* lecture notes — 285 theorems in Lean 4 with Mathlib.

## Getting started

### With Claude Code or Codex

Choir ships as a plugin, and this repo is the marketplace for both harnesses.

In a Claude Code session:

```text
/plugin marketplace add Weber-GeoML/Choir
/plugin install choir@choir
```

In your shell, for Codex:

```text
codex plugin marketplace add Weber-GeoML/Choir
codex plugin add choir@choir
```

Then say what you want in plain language:

- *"Formalize ‹your theorem, paper, or chapter› with Choir"*: start or resume a project as its **overseer**.
- *"Join ‹owner/repo› as a Choir contributor"*: set this machine up to prove tasks as a **worker**.

Explicit invocation works too: `/choir:formalize` and `/choir:join owner/repo`. You can also add your own instructions on top of them.

### With any other agent

The plugin defines no behaviour. Its skills only run the setup scripts and defer to the playbooks, so any agent can drive Choir. Both roles work the same way: read a playbook, call the CLI. Paste the relevant prompt:

**To run a project (overseer):**

> Clone `https://github.com/Weber-GeoML/Choir` to `~/.choir/checkout` if it isn't already there, then read `~/.choir/checkout/docs/agents/ORCHESTRATOR.md` and act as the orchestrator for ‹goal, sources, or owner/repo›. Follow that playbook; don't improvise steps its scripts already own.

**To contribute proofs (worker):**

> Clone `https://github.com/Weber-GeoML/Choir` to `./choir`, run `sh ./choir/scripts/join.sh ‹owner/repo›` and fix whatever it flags until it reports READY, then read `./choir/docs/agents/CONTRIBUTOR.md` and work by it. You do the proving yourself: claim a task, close the placeholder, submit.

The playbooks in [`docs/agents/`](./docs/agents/) cover both roles in full.

## License

Apache-2.0. See [`LICENSE`](./LICENSE).
