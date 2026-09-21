# Conventions for Sample (Rocq)

## Naming

- Theorems/lemmas: `snake_case` (e.g. `one_add_one`, `zero_add_zero`).
- Everything lives in the single `Sample` theory.

## Proof style

- Open with `Proof.` and close with `Qed.` (never leave `Admitted.` in a
  final submission).
- Prefer short tactic proofs (`reflexivity`, `lia`, `auto`, `ring`) when they
  close the goal.
- A brief `(* ... *)` comment on each public theorem.
- Comments say what the code does now, never how it was arrived at.

## What not to use

The verify pipeline catches these; introducing them blocks merge.

- `Axiom` / `Parameter` / `Conjecture` — no new axioms. Blocks until a human
  reviewer approves.
- `native_compute` / `vm_compute` — introduce compilation/runtime trust;
  flagged by axiom-honesty.
- `Unset Universe Checking`, `Declare ML Module` — flagged.
- `Admitted` / `admit` / `Abort` in a final-submission PR — the gate counts
  these and a placeholder on the target declaration blocks merge.

## What the verify pipeline expects

For a `type: prove` task on `target_decl: one_add_one`:

1. After merge, `dune build` must succeed clean.
2. The statement of `one_add_one` (up to the proof) must be **identical**
   between base and head — changing it to make it provable fails the
   statement-equivalence audit.
3. No net-new `Axiom` / `Parameter` / `Conjecture` / `native_compute` /
   `vm_compute`, and no net-new `Admitted` / `admit` / `Abort`.
