"""Tests for `gate.indexer.check`."""

from __future__ import annotations

from gate.indexer.check import (
    find_collisions,
    find_introductions,
    format_findings,
    index_by_last_segment,
)
from gate.indexer.extract import DeclLocation


def _loc(name: str, file: str, line: int = 1) -> DeclLocation:
    return DeclLocation(name=name, file_path=file, line=line)


# ---------------------------------------------------------------------------
# index_by_last_segment
# ---------------------------------------------------------------------------


def test_index_empty() -> None:
    assert index_by_last_segment([]) == {}


def test_index_groups_by_last_segment() -> None:
    decls = [
        _loc("Foo.bar", "a.lean"),
        _loc("Baz.bar", "b.lean"),
        _loc("Foo.qux", "c.lean"),
    ]
    idx = index_by_last_segment(decls)
    assert set(idx.keys()) == {"bar", "qux"}
    assert len(idx["bar"]) == 2
    assert len(idx["qux"]) == 1


# ---------------------------------------------------------------------------
# find_introductions
# ---------------------------------------------------------------------------


def test_no_introductions_when_head_equals_base() -> None:
    base = [_loc("foo", "F.lean")]
    head = [_loc("foo", "F.lean")]
    assert find_introductions(base, head) == []


def test_introductions_finds_new_decls() -> None:
    base = [_loc("foo", "F.lean")]
    head = [_loc("foo", "F.lean"), _loc("bar", "G.lean")]
    intros = find_introductions(base, head)
    assert len(intros) == 1
    assert intros[0].name == "bar"


def test_introductions_identifies_by_file_and_name() -> None:
    # Same name in different files is a new introduction.
    base = [_loc("foo", "A.lean")]
    head = [_loc("foo", "A.lean"), _loc("foo", "B.lean")]
    intros = find_introductions(base, head)
    assert len(intros) == 1
    assert intros[0].file_path == "B.lean"


def test_introductions_removed_decls_are_not_in_intros() -> None:
    # A decl deleted in head is in base-not-head, which isn't an
    # introduction. We only flag head-not-base.
    base = [_loc("foo", "F.lean"), _loc("bar", "G.lean")]
    head = [_loc("foo", "F.lean")]
    assert find_introductions(base, head) == []


# ---------------------------------------------------------------------------
# find_collisions
# ---------------------------------------------------------------------------


def test_no_collisions_when_intros_have_unique_names() -> None:
    base = [_loc("foo", "F.lean")]
    new = [_loc("bar", "G.lean")]
    assert find_collisions(new, base) == []


def test_collision_when_new_name_matches_existing_base_decl() -> None:
    base = [_loc("Existing.foo", "A.lean", line=10)]
    new = [_loc("Brand_new.foo", "B.lean", line=20)]
    findings = find_collisions(new, base)
    assert len(findings) == 1
    assert findings[0].new_decl.name == "Brand_new.foo"
    assert len(findings[0].existing_locations) == 1
    assert findings[0].existing_locations[0].name == "Existing.foo"


def test_collision_multiple_existing_locations() -> None:
    base = [
        _loc("List.length", "List.lean", line=10),
        _loc("String.length", "String.lean", line=20),
    ]
    new = [_loc("Vec.length", "Vec.lean", line=30)]
    findings = find_collisions(new, base)
    assert len(findings) == 1
    assert len(findings[0].existing_locations) == 2


def test_collision_uses_last_segment_only() -> None:
    # `Foo.bar` collides with `Baz.bar` (last segment "bar") even
    # though full names differ.
    base = [_loc("Foo.bar", "F.lean")]
    new = [_loc("Baz.bar", "G.lean")]
    findings = find_collisions(new, base)
    assert len(findings) == 1


def test_no_self_collision() -> None:
    # If the same DeclLocation appeared in both base and head (it
    # wouldn't, by construction of find_introductions, but defensive),
    # don't mark it as a self-collision.
    same = _loc("foo", "F.lean", line=10)
    findings = find_collisions([same], [same])
    assert findings == []


# ---------------------------------------------------------------------------
# format_findings
# ---------------------------------------------------------------------------


def test_format_empty_findings() -> None:
    out = format_findings([])
    assert "No potential duplicates" in out


def test_format_includes_new_and_existing_locations() -> None:
    base = [_loc("Existing.foo", "A.lean", line=10)]
    new = [_loc("Brand_new.foo", "B.lean", line=20)]
    findings = find_collisions(new, base)
    out = format_findings(findings)
    assert "Brand_new.foo" in out
    assert "B.lean:20" in out
    assert "A.lean:10" in out
    assert "informational" in out.lower() or "not blocking" in out.lower()
