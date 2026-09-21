"""Tests for `gate.upgrade.manifest` — the overlay install manifest.

Covers the data model (parse/serialize round-trip, stable output), the
absent-vs-corrupt distinction that keeps a corrupt manifest from silently
restarting retirement tracking, the path safety rails that stop a
hand-edited manifest from deleting files outside the project, and the
retirement decision table.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

import pytest

from gate.upgrade.manifest import (
    CLASS_MANAGED,
    CLASS_SEEDED,
    MANIFEST_VERSION,
    Action,
    Entry,
    ManifestError,
    Retirement,
    build_manifest,
    load_manifest,
    plan_retirements,
    serialize,
    unmanaged_notices,
    validate_relpath,
)

WF = ".github/workflows/"


# ---------------------------------------------------------------------------
# data model
# ---------------------------------------------------------------------------


def test_validate_relpath_accepts_a_plain_relative_path() -> None:
    assert validate_relpath(f"{WF}verify-style.yml") == f"{WF}verify-style.yml"


@pytest.mark.parametrize(
    "bad",
    [
        "/etc/passwd",
        "../../outside.yml",
        ".github/../../outside.yml",
        "",
        ".",
        "./",
    ],
)
def test_validate_relpath_refuses_unsafe_paths(bad: str) -> None:
    with pytest.raises(ManifestError):
        validate_relpath(bad)


def test_build_and_serialize_round_trip() -> None:
    manifest = build_manifest(
        [
            Entry(".choir/verify.toml", CLASS_SEEDED, "b" * 64),
            Entry(f"{WF}verify-style.yml", CLASS_MANAGED, "a" * 64),
        ]
    )
    text = serialize(manifest)
    assert text.endswith("\n")
    again = load_manifest(text)
    assert again.manifest_version == MANIFEST_VERSION
    assert set(again.files) == {".choir/verify.toml", f"{WF}verify-style.yml"}
    assert again.files[f"{WF}verify-style.yml"].cls == CLASS_MANAGED
    assert again.files[".choir/verify.toml"].sha256 == "b" * 64


def test_serialize_is_stable_under_insertion_order() -> None:
    a = Entry(f"{WF}a.yml", CLASS_MANAGED, "1" * 64)
    b = Entry(f"{WF}b.yml", CLASS_MANAGED, "2" * 64)
    assert serialize(build_manifest([a, b])) == serialize(build_manifest([b, a]))


def test_serialize_carries_no_timestamp_or_provenance() -> None:
    """A field that changed every run would make upgrade-project.sh's
    `already up to date` path unreachable and commit an empty upgrade."""
    payload = json.loads(serialize(build_manifest([])))
    assert set(payload) == {"manifest_version", "files"}


def test_load_manifest_rejects_malformed_json() -> None:
    with pytest.raises(ManifestError):
        load_manifest("{not json")


def test_load_manifest_rejects_unknown_manifest_version() -> None:
    with pytest.raises(ManifestError):
        load_manifest(json.dumps({"manifest_version": 99, "files": {}}))


def test_load_manifest_rejects_unknown_class() -> None:
    text = json.dumps(
        {
            "manifest_version": MANIFEST_VERSION,
            "files": {"a.yml": {"class": "vendored", "sha256": "0" * 64}},
        }
    )
    with pytest.raises(ManifestError):
        load_manifest(text)


def test_load_manifest_rejects_unsafe_path_in_files() -> None:
    text = json.dumps(
        {
            "manifest_version": MANIFEST_VERSION,
            "files": {"../../etc/cron.d/x": {"class": "managed", "sha256": "0" * 64}},
        }
    )
    with pytest.raises(ManifestError):
        load_manifest(text)


def test_load_manifest_accepts_an_empty_file_map() -> None:
    text = json.dumps({"manifest_version": MANIFEST_VERSION, "files": {}})
    assert load_manifest(text).files == {}


def test_build_manifest_rejects_an_unknown_class() -> None:
    with pytest.raises(ManifestError):
        build_manifest([Entry("a.yml", "vendored", "0" * 64)])


# ---------------------------------------------------------------------------
# retirement decision table
# ---------------------------------------------------------------------------


def _plan(
    previous_entries: list[Entry],
    overlay: set[str],
    on_disk: Mapping[str, str | None],
) -> list[Retirement]:
    return plan_retirements(build_manifest(previous_entries), overlay, on_disk)


def test_unchanged_overlay_set_retires_nothing() -> None:
    entry = Entry(f"{WF}verify-style.yml", CLASS_MANAGED, "a" * 64)
    assert _plan([entry], {entry.path}, {entry.path: "a" * 64}) == []


def test_managed_file_with_matching_hash_is_deleted() -> None:
    entry = Entry(f"{WF}verify-review.yml", CLASS_MANAGED, "a" * 64)
    (retirement,) = _plan([entry], set(), {entry.path: "a" * 64})
    assert retirement.path == entry.path
    assert retirement.action is Action.DELETE


def test_managed_file_edited_locally_is_reported_not_deleted() -> None:
    entry = Entry(f"{WF}verify-review.yml", CLASS_MANAGED, "a" * 64)
    (retirement,) = _plan([entry], set(), {entry.path: "b" * 64})
    assert retirement.action is Action.REPORT
    assert "edited" in retirement.reason


def test_seeded_file_is_reported_even_when_unmodified() -> None:
    entry = Entry(".choir/review.toml", CLASS_SEEDED, "a" * 64)
    (retirement,) = _plan([entry], set(), {entry.path: "a" * 64})
    assert retirement.action is Action.REPORT
    assert "yours to remove" in retirement.reason


def test_retired_path_already_absent_is_a_silent_no_op() -> None:
    entry = Entry(f"{WF}verify-review.yml", CLASS_MANAGED, "a" * 64)
    (retirement,) = _plan([entry], set(), {entry.path: None})
    assert retirement.action is Action.ALREADY_GONE


def test_retirements_are_ordered_by_path() -> None:
    entries = [
        Entry(f"{WF}z.yml", CLASS_MANAGED, "a" * 64),
        Entry(f"{WF}a.yml", CLASS_MANAGED, "a" * 64),
    ]
    plan = _plan(entries, set(), {e.path: "a" * 64 for e in entries})
    assert [r.path for r in plan] == [f"{WF}a.yml", f"{WF}z.yml"]


def test_a_path_only_in_the_current_overlay_set_is_not_a_retirement() -> None:
    """New files are installs, not retirements — the mechanism is one-directional."""
    assert _plan([], {f"{WF}verify-new.yml"}, {}) == []


# ---------------------------------------------------------------------------
# seed-mode notice
# ---------------------------------------------------------------------------


def test_unmanaged_notices_lists_tracked_files_outside_the_overlay() -> None:
    tracked = [
        f"{WF}verify-style.yml",
        f"{WF}verify-review.yml",
        ".choir/review.toml",
        ".choir/project.toml",
        "README.md",
    ]
    notices = unmanaged_notices(tracked, {f"{WF}verify-style.yml", ".choir/project.toml"})
    assert notices == [".choir/review.toml", f"{WF}verify-review.yml"]


def test_unmanaged_notices_ignores_paths_outside_the_two_directories() -> None:
    assert unmanaged_notices(["README.md", "gate/checks.py"], set()) == []
