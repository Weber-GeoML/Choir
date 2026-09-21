"""Coherence guards for the isabelle/rocq reference sample projects.

They can't be built here (no toolchains installed), so instead of a live
build we check each sample is structurally coherent with the gate: the
declared prover resolves via the same reader the gate uses, the source
carries the profile's placeholder token (so it's a valid Choir-task
target), and the `.choir/` policy files are present.
"""
from __future__ import annotations

from pathlib import Path

from gate.provers import get_profile
from gate.provers.select import read_prover

REPO_ROOT = Path(__file__).resolve().parents[2]
SAMPLES = REPO_ROOT / "samples"


def test_isabelle_sample_declares_isabelle() -> None:
    assert read_prover(SAMPLES / "isabelle") == "isabelle"


def test_rocq_sample_declares_rocq() -> None:
    assert read_prover(SAMPLES / "rocq") == "rocq"


def test_isabelle_sample_source_carries_placeholder() -> None:
    prof = get_profile("isabelle")
    src = (SAMPLES / "isabelle" / "Sample" / "Sample.thy").read_text(encoding="utf-8")
    assert any(tok in src for tok in prof.placeholder_tokens)


def test_rocq_sample_source_carries_placeholder() -> None:
    prof = get_profile("rocq")
    src = (SAMPLES / "rocq" / "Sample" / "Sample.v").read_text(encoding="utf-8")
    assert any(tok in src for tok in prof.placeholder_tokens)


def test_samples_ship_choir_policy() -> None:
    for sample in ("isabelle", "rocq"):
        choir = SAMPLES / sample / ".choir"
        for name in ("project.toml", "verify.toml", "pins.toml"):
            assert (choir / name).is_file(), f"samples/{sample}/.choir/{name} missing"
