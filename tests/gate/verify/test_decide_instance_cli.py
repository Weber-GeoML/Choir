"""Tests for `gate.verify.decide_instance_cli.not_applicable_message`.

`decide_instance` is a lean4-only `extra_audit` (design note 12 §2.2 /
§3.1); the full CLI dispatch (gh/git against a remote PR) is exercised
in the demo, so this pins down the exact not-applicable line the CLI
prints — and passes with exit 0 — for isabelle/rocq projects.
"""

from __future__ import annotations

from gate.verify.decide_instance_cli import not_applicable_message


def test_not_applicable_message_names_the_prover() -> None:
    assert (
        not_applicable_message("isabelle")
        == "decide-instance: not applicable for prover 'isabelle' (lean4-only audit)"
    )


def test_not_applicable_message_for_rocq() -> None:
    assert (
        not_applicable_message("rocq")
        == "decide-instance: not applicable for prover 'rocq' (lean4-only audit)"
    )
