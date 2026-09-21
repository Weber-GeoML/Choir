/-!
# SampleProject

Minimal sample project for Choir's verify pipeline. This file exists so
the orchestrator-side workflows (`issue-intake`, `verify-pr`,
`issue-close-on-merge`) have a real Lean project to fire against during
the demo flow.

The `sorry` on `one_add_one` is intentional. It's the canonical
Choir-task target: an issue references this declaration, a PR closes the
`sorry`, and `verify-pr` rebuilds the project clean-room to confirm.
-/

namespace SampleProject

theorem zero_add_zero : (0 : Nat) + 0 = 0 := rfl

/-- A trivially-true claim left open for the Choir demo task to close. -/
theorem one_add_one : (1 : Nat) + 1 = 2 := by sorry

end SampleProject
