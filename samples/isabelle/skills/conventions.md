# Conventions for Sample (Isabelle)

## Naming

- Lemmas/theorems: `snake_case` (e.g. `one_add_one`, `zero_add_zero`).
- All declarations live in theory `Sample`.

## Proof style

- Prefer a one-line `by <method>` (e.g. `by simp`, `by auto`) when it closes
  the goal.
- Use structured Isar (`proof ... qed`) for multi-step proofs.
- A brief `(* ... *)` comment on each public lemma.
- Comments say what the code does now, never how it was arrived at.

## What not to use

The verify pipeline catches these; introducing them blocks merge.

- `axiomatization` — no new axioms. Blocks until a human reviewer approves.
- `oracle` — introduces external trust; flagged by axiom-honesty.
- `sorry` / `oops` in a final-submission PR — the gate counts these and a
  placeholder on the target declaration blocks merge.

## What the verify pipeline expects

For a `type: prove` task on `target_decl: one_add_one`:

1. After merge, `isabelle build -D .` must succeed clean.
2. The statement of `one_add_one` (up to the proof) must be **identical**
   between base and head — changing it to make it provable fails the
   statement-equivalence audit.
3. No net-new `axiomatization` / `oracle`, and no net-new `sorry` / `oops`.
