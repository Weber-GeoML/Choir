# Sample (Rocq) — skill pack

This `skills/` directory is the project-specific knowledge layer Choir
transfers to every contributor agent via the standard workspace clone. This
minimal pack illustrates the format for a Rocq project; a real Choir-managed
project would carry substantially more.

## What this project is

`samples/rocq/` is a minimal Rocq (Coq) project — Choir's Rocq reference
target. It exists to exercise the gate's verify pipeline end-to-end; it is
not a serious mathematical artefact.

`one_add_one` is left `Admitted`; a Choir task asks contributors to replace
it with a complete proof closed by `Qed`.

## Scope

- Single dune coq theory `Sample` (see `dune` / `_CoqProject`), single file
  `Sample.v`.
- Rocq standard library only — no opam packages beyond `rocq-prover`, so
  `dune build` stays fast on CI.

## Dependencies and pins

| Pin | Where | Value |
|---|---|---|
| rocq-prover | `project_ref.toolchain` (per task) | e.g. `9.0.0` (opam) |
| external opam libs | (not used) | — |

If your task requires an external opam library, it is mis-scoped — open an
issue rather than adding the dependency.

## Where to find things

- `Sample.v` — the single source file
- `dune` / `dune-project` / `_CoqProject` — the build configuration
- `skills/` — you are here
