---
name: purge
description: Remove this machine's local Choir state for a project, or all of it
user_invocable: true
argument-hint: '<owner/name> | all'
---

# Choir Purge

Removes what Choir created under `~/.choir` for a project: its workspaces,
its shared clone, its build cache, and its per-project config. Your checkout,
your fork, and the project on GitHub are untouched.

## Procedure

1. Run `choir purge <target> --dry-run` and show
   the user the output verbatim. It lists every path, what it reclaims, the
   workspaces in scope, and whether the real run would refuse.
2. Ask the user to confirm. Do not skip this — the deletion is not reversible
   and may include proof work that was never submitted.
3. On confirmation, run `choir purge <target>`.
4. If it refuses, relay the refusal as printed. It names each workspace and
   its remedy — usually `choir worker release <owner/name> <issue>` for a claim
   the user still holds, or `--force` when the work is already submitted and
   they only want the disk back. Do not pass `--force` on your own initiative;
   ask.

`choir purge all` additionally removes the shared dependency store, the
machine config, and the orchestrator config — the clean-slate reset. It does
not remove the `choir` tool: that lives in the checkout you installed it from,
at `.venv/bin/choir`, and goes away only when you delete that checkout.
