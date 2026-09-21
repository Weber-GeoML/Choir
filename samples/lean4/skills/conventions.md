# Conventions for SampleProject

## Naming

- Theorems: `snake_case` (e.g., `one_add_one`, `zero_add_zero`).
- Definitions: `camelCase` for terms; `PascalCase` for types.
- All top-level declarations live in `namespace SampleProject ... end SampleProject`.

## Proof style

- Prefer term-mode proofs (`:= rfl`, `:= proof_term`) when the proof is
  one expression.
- Use `by tactic` blocks for multi-step proofs.
- One blank line between top-level declarations.
- Docstring comments (`/-- ... -/`) on every public theorem.
- Comments say what the code does now, never how it was arrived at.

## What not to use

The verify pipeline catches these; introducing them blocks merge.

- `axiom` — no new axioms. Audit blocks merge until human reviewer
  approves.
- `native_decide` — introduces runtime trust; flagged by axiom-honesty.
- `decide` — when used on non-computable propositions, equivalent to
  axiom. Flagged.
- `partial def` — flagged.
- `unsafe def` — flagged.
- `sorry` in final-submission PRs — `lake build` warns but doesn't fail;
  the verify pipeline counts these and a sorry on `target_decl` in the
  head version blocks merge.

## What the verify pipeline expects

For a task with `type: prove` on `target_decl: SampleProject.foo`:

1. After your PR is merged, `SampleProject.lean` must `lake build`
   clean.
2. The signature of `foo` (the part from `theorem foo` up to `:=`)
   must be **identical** between base and head — modulo whitespace.
   Changing the type to make it provable fails the
   statement-equivalence audit.
3. No new entries in any of: `axiom`, `unsafe`, `partial`,
   `native_decide`, `extern`.

If you stick to these conventions, the deterministic audits pass.
The project's orchestrator reviews every PR itself before merging —
there's no separate review-task config to consult.
