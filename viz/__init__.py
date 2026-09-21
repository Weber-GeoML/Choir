"""Proof-tree timeline: derive a project's history and render it.

Task management, not verification. A node's state is read from the source
text by pattern match — a declaration counts as clear when the prover's
placeholder token is gone from its body — and the dependencies it is
judged against are whatever the roadmap declares. Neither is a kernel
check, and an unrecorded edge is invisible, so a node can read as clear
while resting on something unfinished. The gate is what verifies a
project; this shows how the work moved.

Read-only. Nothing here writes to a project repo.
"""
