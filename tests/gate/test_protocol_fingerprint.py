"""Tests for the wire-fingerprint guard (design note 13 §2.1).

This is the mechanical enforcement of the `PROTOCOL_VERSION` bump rules:
if any of the surfaces `compute_fingerprint()` hashes drifts from the
committed `protocol_fingerprint.json`, the suite fails with the
actionable instruction block, rather than shipping an unnoticed wire
change. See `gate/protocol_fingerprint.py`'s module docstring for what
counts as a "wire surface."
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, Field

import gate.protocol_fingerprint as pf
from gate.protocol import PROTOCOL_VERSION
from gate.state import reduction_record as reduction_record_mod

EXPECTED_SURFACE_NAMES = {
    "task_record",
    "labels",
    "verify_config",
    "project_toml_contract",
    "lease_conventions",
    "required_checks",
    "prover_registry",
    "reduction_record",
}


# ---------------------------------------------------------------------------
# Stored snapshot — exists, parses, matches PROTOCOL_VERSION
# ---------------------------------------------------------------------------


def test_stored_file_exists_and_parses() -> None:
    assert pf.STORED_PATH.is_file()
    stored = pf.load_stored()
    assert isinstance(stored, dict)
    assert "protocol_version" in stored
    assert "surfaces" in stored
    assert isinstance(stored["surfaces"], dict)


def test_stored_protocol_version_matches() -> None:
    stored = pf.load_stored()
    assert stored["protocol_version"] == PROTOCOL_VERSION, pf.format_version_mismatch_message(
        stored["protocol_version"], PROTOCOL_VERSION
    )


def test_stored_surface_names_are_exactly_the_expected_set() -> None:
    stored = pf.load_stored()
    assert set(stored["surfaces"]) == EXPECTED_SURFACE_NAMES


# ---------------------------------------------------------------------------
# The guard itself: compute_fingerprint() must match the committed snapshot
# ---------------------------------------------------------------------------


def test_computed_fingerprint_matches_stored_snapshot() -> None:
    stored = pf.load_stored()
    computed = pf.compute_fingerprint()
    added, removed, changed = pf.diff_surfaces(computed, stored["surfaces"])
    assert computed == stored["surfaces"], pf.format_mismatch_message(
        added, removed, changed
    )


def test_computed_surface_names_are_exactly_the_expected_set() -> None:
    computed = pf.compute_fingerprint()
    assert set(computed) == EXPECTED_SURFACE_NAMES


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_compute_fingerprint_is_deterministic() -> None:
    first = pf.compute_fingerprint()
    second = pf.compute_fingerprint()
    assert first == second


# ---------------------------------------------------------------------------
# Tamper test: a change to exactly one surface's source is attributed to
# exactly that surface, and nothing else — proves per-surface attribution
# isn't vacuous (e.g. everything hashed into one blob).
# ---------------------------------------------------------------------------


def test_tampering_one_surface_is_attributed_to_only_that_surface(monkeypatch) -> None:
    baseline = pf.compute_fingerprint()

    monkeypatch.setattr(pf.stale_claims_mod, "HEARTBEAT_PREFIX", "tampered/")

    tampered = pf.compute_fingerprint()
    added, removed, changed = pf.diff_surfaces(tampered, baseline)

    assert added == []
    assert removed == []
    assert changed == ["lease_conventions"]


def test_format_mismatch_message_names_every_changed_surface() -> None:
    msg = pf.format_mismatch_message(["a"], ["b"], ["c"])
    assert "a" in msg
    assert "b" in msg
    assert "c" in msg
    assert "design note 13 §2" in msg
    assert "PROTOCOL_VERSION" in msg
    assert "gate.protocol_fingerprint --update" in msg


# ---------------------------------------------------------------------------
# Version-only mismatch: a stored snapshot whose surfaces still all match
# but whose `protocol_version` is stale must be exactly as actionable as a
# surface mismatch — both trigger conditions emit the full two-option
# instruction block (design note 13 §2, `--update`, and — for the version
# case — both version numbers), never a bare note or a generic assert.
# ---------------------------------------------------------------------------


def test_format_version_mismatch_message_names_trigger_and_both_versions() -> None:
    msg = pf.format_version_mismatch_message(999, PROTOCOL_VERSION)
    assert "999" in msg
    assert str(PROTOCOL_VERSION) in msg
    assert "protocol_version mismatch" in msg
    assert "design note 13 §2" in msg
    assert "PROTOCOL_VERSION" in msg
    assert "gate.protocol_fingerprint --update" in msg


def test_print_comparison_version_only_mismatch_emits_instruction_block(
    monkeypatch, capsys
) -> None:
    """Reproduces the bug: surfaces untouched, `protocol_version` wrong.

    Before the fix, `_print_comparison` printed only a bare "note:" line
    and fell through to `return 1` with no instruction block at all.
    """
    stored = pf.load_stored()
    tampered_stored = {**stored, "protocol_version": 999}
    monkeypatch.setattr(pf, "load_stored", lambda: tampered_stored)

    exit_code = pf._print_comparison()
    out = capsys.readouterr().out

    assert exit_code == 1
    assert "protocol_version mismatch" in out
    assert "999" in out
    assert str(PROTOCOL_VERSION) in out
    assert "design note 13 §2" in out
    assert "gate.protocol_fingerprint --update" in out


# ---------------------------------------------------------------------------
# required_checks extraction fails loud on ambiguity, instead of silently
# taking the first "contexts" block in the file (which could be an
# unrelated comment).
# ---------------------------------------------------------------------------


def test_parse_required_checks_raises_on_ambiguous_contexts_block() -> None:
    real_text = pf.NEW_PROJECT_SCRIPT.read_text(encoding="utf-8")
    decoy_text = real_text + '\n# example: {"contexts": ["decoy-check"]}\n'

    with pytest.raises(pf.ProtocolFingerprintError, match="ambiguous"):
        pf._parse_required_checks(decoy_text)


def test_parse_required_checks_raises_on_missing_contexts_block() -> None:
    with pytest.raises(pf.ProtocolFingerprintError, match="ambiguous"):
        pf._parse_required_checks("no contexts array anywhere in this text")


def test_parse_required_checks_matches_real_script_unambiguously() -> None:
    real_text = pf.NEW_PROJECT_SCRIPT.read_text(encoding="utf-8")
    assert pf._parse_required_checks(real_text) == pf._required_checks_surface()


# ---------------------------------------------------------------------------
# labels extraction ignores commented-out `gh label create "..."` lines.
# ---------------------------------------------------------------------------


def test_parse_label_vocabulary_ignores_commented_lines() -> None:
    text = (
        '# gh label create "choir/decoy-in-a-comment"\n'
        'gh label create "choir/real-label" --repo "$REPO" --force\n'
    )
    assert pf._parse_label_vocabulary(text) == ["choir/real-label"]


def test_parse_label_vocabulary_decoy_comment_does_not_change_real_script_surface() -> None:
    real_text = pf.SETUP_LABELS_SCRIPT.read_text(encoding="utf-8")
    decoy_text = real_text + '\n# gh label create "choir/decoy-only-in-a-comment"\n'

    assert pf._parse_label_vocabulary(decoy_text) == pf._parse_label_vocabulary(real_text)


# ---------------------------------------------------------------------------
# Individual surface builders don't blow up and return non-empty content
# (a light sanity net on top of the hash-level guard above).
# ---------------------------------------------------------------------------


def test_required_checks_surface_is_nonempty() -> None:
    assert pf._required_checks_surface()


def test_labels_surface_vocabulary_is_nonempty() -> None:
    surface = pf._labels_surface()
    assert surface["label_vocabulary"]
    assert "choir/available" in surface["label_vocabulary"]


def test_prover_registry_surface_includes_lean4() -> None:
    assert "lean4" in pf._prover_registry_surface()


# ---------------------------------------------------------------------------
# reduction_record — reduction block's wire shape
# ---------------------------------------------------------------------------


def test_reduction_record_is_a_fingerprinted_surface() -> None:
    surfaces = pf.compute_fingerprint()
    assert "reduction_record" in surfaces


def test_reduction_surface_names_its_fields_and_tag() -> None:
    surface = pf._reduction_record_surface()
    assert surface["block_tag"] == "choir-reduction"
    assert surface["version"] == 1
    assert "parent" in surface["fields"]
    assert "children" in surface["fields"]
    assert "decl" in surface["child_fields"]


def test_pydantic_wire_field_names_uses_aliases() -> None:
    """Wire field names are extracted as aliases where set, attribute names otherwise.

    This tests the helper directly against a throwaway model with mixed aliased and
    un-aliased fields, ensuring it returns wire names (aliases) where they exist.
    """

    class MixedModel(BaseModel):
        python_attr_no_alias: str
        python_with_alias: str = Field(alias="wire-alias")
        another_no_alias: int

    names = pf._pydantic_wire_field_names(MixedModel)
    assert names == ["another_no_alias", "python_attr_no_alias", "wire-alias"]
    # Verify the helper prefers aliases
    assert "python_with_alias" not in names
    assert "wire-alias" in names


def test_reduction_surface_calls_helper_for_child_fields(monkeypatch) -> None:
    """The surface must call the helper to read child field wire names.

    This is an integration test: it patches a throwaway ReductionChild model
    into the reduction_record_mod namespace, calls _reduction_record_surface(),
    and verifies child_fields reflects the helper's output (alias where set, not
    Python name). The test would fail if the call site bypassed the helper.
    """

    class PatchedChild(BaseModel):
        normal_field: str
        aliased_field: str = Field(alias="wire-alias")

    monkeypatch.setattr(reduction_record_mod, "ReductionChild", PatchedChild)

    surface = pf._reduction_record_surface()
    # If the helper is called, child_fields will contain the alias
    assert "wire-alias" in surface["child_fields"]
    # If the call site bypassed the helper and sorted raw keys, only the Python name would be there
    assert "aliased_field" not in surface["child_fields"]
