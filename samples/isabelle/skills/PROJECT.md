# Sample (Isabelle) — skill pack

This `skills/` directory is the project-specific knowledge layer Choir
transfers to every contributor agent via the standard workspace clone. This
minimal pack illustrates the format for an Isabelle project; a real
Choir-managed project would carry substantially more.

## What this project is

`samples/isabelle/` is a minimal Isabelle/HOL project — Choir's Isabelle
reference target. It exists to exercise the gate's verify pipeline
end-to-end; it is not a serious mathematical artefact.

`Sample.one_add_one` carries a `sorry` that a Choir task asks contributors to
replace with a complete proof.

## Scope

- Single session `Sample` (see `ROOT`), single theory `Sample.thy`.
- `imports Main` only — no AFP, so `isabelle build` stays fast on CI.

## Dependencies and pins

| Pin | Where | Value |
|---|---|---|
| Isabelle release | `project_ref.toolchain` (per task) | e.g. `Isabelle2025` |
| AFP | (not used) | — |

If your task requires AFP, it is mis-scoped — open an issue rather than
adding the dependency.

## Where to find things

- `Sample.thy` — the single theory
- `ROOT` — the session definition
- `skills/` — you are here
