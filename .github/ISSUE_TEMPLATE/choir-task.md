---
name: Choir task
about: A new task for a contributor agent to claim and work.
labels: ["choir/available"]
title: "prove: `<target_decl>`"
---

<!--
This is a Choir task. The YAML block below is the machine-readable
contract; the prose after it is what the contributor's agent reads
as `TASK.md`. Replace placeholders (OWNER/REPO, SHA, target_*, etc.)
before submitting.
-->

---
choir-task-version: 1
type: prove
target_file: samples/lean4/SampleProject.lean
target_decl: SampleProject.one_add_one
project_ref:
  repo: OWNER/REPO
  commit: PASTE_HEAD_SHA_HERE
  toolchain: leanprover/lean4:v4.32.0
deps: []
---

## Statement

Replace this with the actual theorem statement. For the demo, the existing
`SampleProject.one_add_one` in `samples/lean4/SampleProject.lean` has a
`sorry` you can close:

```lean
theorem one_add_one : (1 : Nat) + 1 = 2 := by sorry
```

## Notes for the prover

Any extra context goes here — proof-strategy hints, references, why the
statement is what it is. Plain prose; the agent reads this verbatim.
