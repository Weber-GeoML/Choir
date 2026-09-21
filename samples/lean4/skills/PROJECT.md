# SampleProject — skill pack

This `skills/` directory is the project-specific knowledge layer that
Choir transfers to every contributor agent via the standard workspace
clone. Real Choir-managed projects would have substantially more
content here; for the demo project, this minimal pack illustrates the
format.

## What this project is

`samples/lean4/` is a minimal Lean 4 project used as Choir's demo
target. It exists to exercise the orchestrator-side verify pipeline
end-to-end; it is not a serious mathematical artefact.

The library contains a handful of trivial theorems, one of which
(`SampleProject.one_add_one`) carries a `sorry` that Choir tasks ask
contributors to close.

## Scope

- Single Lean library named `SampleProject`.
- Single file at `SampleProject.lean`.
- No external dependencies — no Mathlib in v0 (we want verify-pr to
  run in under a minute on a fresh CI runner).

## Dependencies and pins

| Pin | Where | Value |
|---|---|---|
| Lean toolchain | `lean-toolchain` | `leanprover/lean4:v4.32.0` |
| Mathlib | (not used) | — |
| lake-manifest | (not committed) | generated on first build |

If your task requires Mathlib, the task is mis-scoped. Open an issue
rather than adding the dependency.

## Where to find things

- `SampleProject.lean` — the single library file
- `lakefile.toml` — Lake configuration
- `lean-toolchain` — pinned Lean version
- `skills/` — you are here

## Where the blueprint lives

This demo has no blueprint. Real projects (mathlib-auto, textbook
formalizations, etc.) would have a `blueprint.md` here pointing at the
LaTeX or Markdown source the formalizations are derived from.
