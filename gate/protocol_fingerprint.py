"""Wire-fingerprint guard — mechanical enforcement of design note 13 §2.

Design note 13 §2 states the `PROTOCOL_VERSION` bump rules as prose:
bump when a change alters what parties (overseer, workers, gate) must
agree on — `TaskRecord` fields/semantics, lifecycle/priority/difficulty
label vocabulary, `.choir/*` config schemas, lease/branch conventions,
the workflow<->CLI contract. This module makes that
discipline mechanical: `compute_fingerprint()` hashes every such wire
surface, `tests/gate/test_protocol_fingerprint.py` compares the hash
against the committed `protocol_fingerprint.json`, and any drift fails
the suite with instructions instead of silently shipping.

Usage::

    uv run python -m gate.protocol_fingerprint              # compare, exit 0/1
    uv run python -m gate.protocol_fingerprint --update      # regenerate the snapshot

Stability: `compute_fingerprint()` must return the same hashes on every
run against the same code — no timestamps, no absolute paths, no
dict-order dependence. Every surface is built from stable data
(pydantic `model_json_schema()`, sorted enum values, sorted dataclass
field names, literal source text) and hashed via
`json.dumps(..., sort_keys=True, ensure_ascii=True)`.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import re
import sys
from enum import Enum
from pathlib import Path

from client import workspace as workspace_mod
from gate import provers as provers_mod
from gate.protocol import PROTOCOL_VERSION
from gate.reconcile import stale_claims as stale_claims_mod
from gate.state import labels as labels_mod
from gate.state import lease_comment as lease_comment_mod
from gate.state import reduction_record as reduction_record_mod
from gate.state import task_record as task_record_mod
from gate.verify import config as verify_config_mod

REPO_ROOT = Path(__file__).resolve().parent.parent
SETUP_LABELS_SCRIPT = REPO_ROOT / "scripts" / "setup-labels.sh"
NEW_PROJECT_SCRIPT = REPO_ROOT / "scripts" / "new-project.sh"

STORED_PATH = Path(__file__).parent / "protocol_fingerprint.json"

# Extracted literals — see the corresponding source modules cited in each
# docstring below. Hardcoded rather than imported because the source
# doesn't expose these as named constants; if the source ever changes,
# these must be updated by hand (and the mismatch this guard produces
# elsewhere will not, by itself, catch drift in these two literals). Same
# caveat applies to AUTOMATION_MERGE_VALUES below: this keeps `gate/`
# free of any import of `orchestrator/` (gate code runs inside a
# project's CI against untrusted PRs and must not depend on overseer-side
# tooling — see `gate/reconcile/config.py`'s docstring), at the cost that
# drift between this literal and `orchestrator.project_config.
# MergeAutomation`'s values is caught by nothing — accepted, same as the
# other literals here, because the automation-level vocabulary is
# human-gated and slow-moving.
LEASE_BRANCH_PREFIX = "choir/"  # client/workspace.py: f"choir/{issue}-{slug}"
LEASE_METADATA_FILENAME = ".choir-lease.json"  # client/workspace.py workspace layout
AUTOMATION_MERGE_VALUES = ("approve", "auto", "manual")  # orchestrator's MergeAutomation

# The keys the substrate reads from `.choir/project.toml` (design note 13
# §3, orchestrator/project_config.py, gate/reconcile/config.py). This both
# fingerprints and documents the contract.
PROJECT_TOML_CONTRACT: dict[str, list[str]] = {
    "project": ["prover", "choir_protocol", "choir_commit"],
    "automation": ["merge"],
    "reconcile": ["stale_after_days"],
}

_LABEL_CREATE_RE = re.compile(r'gh label create "([^"]+)"')
_CONTEXTS_BLOCK_RE = re.compile(r'"contexts":\s*\[(.*?)\]', re.DOTALL)
_QUOTED_RE = re.compile(r'"([^"]+)"')


class ProtocolFingerprintError(RuntimeError):
    """A wire surface could not be read/parsed while computing the fingerprint."""


def _hash_surface(value: object) -> str:
    canonical = json.dumps(value, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _dataclass_to_jsonable(obj: object) -> object:
    """Recursively turn a (possibly nested) frozen dataclass/Enum into JSON-safe data.

    Dict keys end up being the field names, so this doubles as a field-name
    fingerprint (the surface changes if a field is renamed/added/removed)
    and a defaults fingerprint (the values are whatever the no-arg
    constructor produces).
    """
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {
            f.name: _dataclass_to_jsonable(getattr(obj, f.name))
            for f in dataclasses.fields(obj)
        }
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, (tuple, list)):
        return [_dataclass_to_jsonable(x) for x in obj]
    return obj


# ---------------------------------------------------------------------------
# task_record — TaskRecord/ProjectRef schema + TaskType vocabulary
# ---------------------------------------------------------------------------


def _task_record_surface() -> dict[str, object]:
    return {
        "TaskRecord": task_record_mod.TaskRecord.model_json_schema(),
        "ProjectRef": task_record_mod.ProjectRef.model_json_schema(),
        "TaskType_values": sorted(t.value for t in task_record_mod.TaskType),
    }


# ---------------------------------------------------------------------------
# labels — lifecycle/type vocabulary (scripts/setup-labels.sh) + priority/
# difficulty (gate.state.labels)
# ---------------------------------------------------------------------------


def _parse_label_vocabulary(text: str) -> list[str]:
    """Extract the `gh label create "..."` vocabulary from script text.

    Lines whose stripped form starts with `#` are skipped, so a doc
    comment mentioning `gh label create "..."` as an example doesn't
    contaminate the real vocabulary the script would actually create.
    """
    names: list[str] = []
    for line in text.splitlines():
        if line.strip().startswith("#"):
            continue
        names.extend(_LABEL_CREATE_RE.findall(line))
    if not names:
        raise ProtocolFingerprintError(
            f"no 'gh label create \"...\"' lines found in {SETUP_LABELS_SCRIPT}"
        )
    return sorted(set(names))


def _read_setup_labels_vocabulary() -> list[str]:
    text = SETUP_LABELS_SCRIPT.read_text(encoding="utf-8")
    return _parse_label_vocabulary(text)


def _labels_surface() -> dict[str, object]:
    return {
        "label_vocabulary": _read_setup_labels_vocabulary(),
        "priority_prefix": labels_mod.PRIORITY_PREFIX,
        "difficulty_prefix": labels_mod.DIFFICULTY_PREFIX,
        "priority_names": sorted(p.name for p in labels_mod.Priority),
        "difficulty_names": sorted(d.name for d in labels_mod.Difficulty),
    }


# ---------------------------------------------------------------------------
# verify_config — .choir/verify.toml policy enums + config shape/defaults
# ---------------------------------------------------------------------------


def _verify_config_surface() -> dict[str, object]:
    return {
        "axiom_policy_values": sorted(p.value for p in verify_config_mod.AxiomPolicy),
        "sorry_policy_values": sorted(p.value for p in verify_config_mod.SorryPolicy),
        # Field names AND defaults in one shot: the dict keys are the
        # dataclass field names (VerifyConfig + its nested
        # AxiomHonestyConfig/SorryDeltaConfig/StyleConfig), the values are
        # what the no-arg constructors produce.
        "default_verify_config": _dataclass_to_jsonable(verify_config_mod.VerifyConfig()),
    }


# ---------------------------------------------------------------------------
# project_toml_contract — the literal .choir/project.toml key contract
# ---------------------------------------------------------------------------


def _project_toml_contract_surface() -> dict[str, object]:
    return {
        "keys": PROJECT_TOML_CONTRACT,
        "automation_merge_values": sorted(AUTOMATION_MERGE_VALUES),
    }


# ---------------------------------------------------------------------------
# lease_conventions — branch prefix, lease filename, LeaseMetadata fields,
# heartbeat label prefix
# ---------------------------------------------------------------------------


def _lease_conventions_surface() -> dict[str, object]:
    return {
        "branch_prefix": LEASE_BRANCH_PREFIX,
        "lease_filename": LEASE_METADATA_FILENAME,
        "lease_metadata_fields": sorted(
            f.name for f in dataclasses.fields(workspace_mod.LeaseMetadata)
        ),
        # Read from the gate's reconcile sweep, which is the module that
        # still *interprets* this label. Spec D4 stopped the client from
        # writing it (a heartbeat is now an edit to the worker's own lease
        # comment — `client/heartbeat.py`), but the label stays part of the
        # vocabulary a reader must recognize: pre-D4 issues carry them, and
        # `gate/reconcile/stale_claims.py` reads them to decide staleness.
        # The fingerprint hashes the vocabulary, not who writes it.
        "heartbeat_prefix": stale_claims_mod.HEARTBEAT_PREFIX,
        # The lease comment format itself (spec D4). This is the surface a
        # claim is *made* on now that a contributor has no write access to
        # self-assign or move a label, so it is exactly the kind of thing
        # both parties must agree on: the client renders it, and both the
        # client and the orchestrator parse it to decide who holds a lease.
        # A silent change to the tag or the action vocabulary would leave a
        # newer worker's claims invisible to an older orchestrator, which is
        # the failure the pin exists to catch.
        #
        # The block's *field* names are not hashed on purpose. An unknown
        # extra field is forward-compatible by design (`parse_lease_comment`
        # ignores it), so adding one is not a change the parties must agree
        # on — whereas an unknown *action* is refused, which is why the
        # action vocabulary is here.
        "lease_block_tag": lease_comment_mod.LEASE_BLOCK_TAG,
        "lease_actions": sorted(lease_comment_mod._VALID_ACTIONS),
    }


# ---------------------------------------------------------------------------
# required_checks — branch-protection contexts (scripts/new-project.sh)
# ---------------------------------------------------------------------------


def _parse_required_checks(text: str) -> list[str]:
    """Extract the branch-protection `contexts` array from script text.

    Requires exactly one match: `.search` (first-match) would silently
    prefer an unrelated `"contexts": [...]` earlier in the file (e.g. in
    a comment) over the real required_status_checks block, replacing the
    fingerprinted check list with garbage instead of failing loud.
    """
    matches = _CONTEXTS_BLOCK_RE.findall(text)
    if len(matches) != 1:
        raise ProtocolFingerprintError(
            f"ambiguous/missing contexts block in {NEW_PROJECT_SCRIPT} — "
            f"fingerprint extraction needs exactly one, found {len(matches)}"
        )
    contexts = _QUOTED_RE.findall(matches[0])
    if not contexts:
        raise ProtocolFingerprintError(
            f"'contexts' array in {NEW_PROJECT_SCRIPT} parsed as empty"
        )
    return sorted(contexts)


def _required_checks_surface() -> list[str]:
    text = NEW_PROJECT_SCRIPT.read_text(encoding="utf-8")
    return _parse_required_checks(text)


# ---------------------------------------------------------------------------
# prover_registry — registry names (wire: they appear in project.toml)
# ---------------------------------------------------------------------------


def _prover_registry_surface() -> list[str]:
    return sorted(provers_mod.PROFILES)


# ---------------------------------------------------------------------------
# reduction_record — reduction block's wire shape (tag, version, field names)
# ---------------------------------------------------------------------------


def _pydantic_wire_field_names(model_class: type) -> list[str]:
    """Extract sorted wire field names from a pydantic model class.

    Returns the field names as they appear on the wire: field alias where one is set,
    Python attribute name otherwise. Wire names are what the opposite party (worker or
    orchestrator) sees and must agree on — a rename on one side without the other is
    a compatibility break the fingerprint catches. This function centralizes that
    name-reading logic so it is used consistently across all surfaces.
    """
    return sorted(
        f.alias or name
        for name, f in model_class.model_fields.items()
    )


def _reduction_record_surface() -> dict[str, object]:
    """The reduction block's wire shape: tag, version, field names.

    Worker-written and orchestrator-read, so a rename on one side
    without the other is a compatibility break the fingerprint catches.
    """
    return {
        "block_tag": reduction_record_mod.REDUCTION_BLOCK_TAG,
        "version": 1,
        "fields": _pydantic_wire_field_names(reduction_record_mod.ReductionRecord),
        "child_fields": _pydantic_wire_field_names(reduction_record_mod.ReductionChild),
    }


# ---------------------------------------------------------------------------
# compute_fingerprint — the public entry point
# ---------------------------------------------------------------------------


def compute_fingerprint() -> dict[str, str]:
    """Sha256 fingerprint of every wire surface Choir's parties must agree on.

    See the module docstring for what "wire surface" means and design note
    13 §2 for the bump discipline this backs.
    """
    surfaces: dict[str, object] = {
        "task_record": _task_record_surface(),
        "labels": _labels_surface(),
        "verify_config": _verify_config_surface(),
        "project_toml_contract": _project_toml_contract_surface(),
        "lease_conventions": _lease_conventions_surface(),
        "required_checks": _required_checks_surface(),
        "prover_registry": _prover_registry_surface(),
        "reduction_record": _reduction_record_surface(),
    }
    return {name: _hash_surface(value) for name, value in surfaces.items()}


def load_stored() -> dict[str, object]:
    """Load and parse the committed `protocol_fingerprint.json`."""
    return json.loads(STORED_PATH.read_text(encoding="utf-8"))


def diff_surfaces(
    computed: dict[str, str], stored: dict[str, str]
) -> tuple[list[str], list[str], list[str]]:
    """Return `(added, removed, changed)` surface names, each sorted."""
    added = sorted(set(computed) - set(stored))
    removed = sorted(set(stored) - set(computed))
    changed = sorted(
        name
        for name in set(computed) & set(stored)
        if computed[name] != stored[name]
    )
    return added, removed, changed


_INSTRUCTION_BLOCK = (
    "Decide per design note 13 §2: (a) round-trip COMPATIBLE change → "
    "regenerate via `uv run python -m gate.protocol_fingerprint --update` "
    "and justify compatibility in the commit message; (b) INCOMPATIBLE "
    "change → bump PROTOCOL_VERSION in gate/protocol.py, THEN regenerate, "
    "and record the new version's meaning in design note 13 §2."
)


def format_mismatch_message(
    added: list[str], removed: list[str], changed: list[str]
) -> str:
    """The actionable instruction block a fingerprint mismatch must show."""
    names = sorted({*added, *removed, *changed})
    names_str = ", ".join(names)
    return f"Wire surface(s) changed: {names_str}. {_INSTRUCTION_BLOCK}"


def format_version_mismatch_message(stored_version: object, current_version: object) -> str:
    """The actionable instruction block a `protocol_version`-only mismatch must show.

    Same trigger→instruction shape as `format_mismatch_message`, adapted to
    name the version mismatch (rather than a surface list) as the trigger —
    a stored snapshot can go stale by protocol_version alone even when every
    hashed surface still matches (e.g. someone hand-edited the JSON, or a
    revert left the version behind).
    """
    return (
        f"protocol_version mismatch: stored={stored_version!r} "
        f"current(gate.protocol.PROTOCOL_VERSION)={current_version!r}. "
        f"{_INSTRUCTION_BLOCK}"
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_comparison() -> int:
    try:
        stored = load_stored()
    except FileNotFoundError:
        print(
            f"no stored fingerprint at {STORED_PATH} — run with --update", file=sys.stderr
        )
        return 1

    stored_version = stored.get("protocol_version")
    version_mismatch = stored_version != PROTOCOL_VERSION

    computed = compute_fingerprint()
    stored_surfaces = stored.get("surfaces", {})
    added, removed, changed = diff_surfaces(computed, stored_surfaces)
    surface_mismatch = bool(added or removed or changed)

    if not version_mismatch and not surface_mismatch:
        print("protocol fingerprint OK — every wire surface matches the stored snapshot.")
        return 0

    if version_mismatch:
        print(format_version_mismatch_message(stored_version, PROTOCOL_VERSION))

    if surface_mismatch:
        print(format_mismatch_message(added, removed, changed))
        for name in added:
            print(f"  + {name} (new surface, absent from the stored snapshot)")
        for name in removed:
            print(f"  - {name} (surface removed; stored snapshot has a stale entry)")
        for name in changed:
            print(
                f"  ~ {name}: stored={stored_surfaces[name][:12]}... "
                f"computed={computed[name][:12]}..."
            )
    return 1


def _write_update() -> int:
    computed = compute_fingerprint()
    payload = {
        "protocol_version": PROTOCOL_VERSION,
        "surfaces": dict(sorted(computed.items())),
    }
    STORED_PATH.write_text(
        json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {STORED_PATH}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m gate.protocol_fingerprint",
        description=(
            "Compare (or regenerate) the wire-protocol fingerprint "
            "backing design note 13 §2's bump discipline."
        ),
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="regenerate protocol_fingerprint.json from the current code",
    )
    args = parser.parse_args(argv)
    if args.update:
        return _write_update()
    return _print_comparison()


if __name__ == "__main__":
    sys.exit(main())
