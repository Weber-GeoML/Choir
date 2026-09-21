"""Tests for `gate.verify.changed_decls` (design note 12 §4.1).

Mirrors `tests/gate/provers/test_select.py`'s tmp-git-repo pattern:
build a real repo, commit a base state, mutate the worktree at head
(no second commit needed — `detect_changed_decls` diffs the base SHA
against the workspace's current checkout, matching a PR-head
checkout), and assert on the detected targets.
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest

from gate.provers.isabelle import ISABELLE
from gate.provers.lean4 import LEAN4
from gate.provers.rocq import ROCQ
from gate.verify import changed_decls
from gate.verify.changed_decls import (
    ChangedDecl,
    ChangedDeclsError,
    detect_changed_decls,
)


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return result.stdout


def _init_repo(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")


def _commit_base(tmp_path: Path, message: str = "base") -> str:
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", message)
    return _git(tmp_path, "rev-parse", "HEAD").strip()


# ---------------------------------------------------------------------------
# New declaration
# ---------------------------------------------------------------------------


def test_new_decl_is_detected(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "F.lean").write_text("theorem foo : 1 = 1 := rfl\n", encoding="utf-8")
    base_sha = _commit_base(tmp_path)

    (tmp_path / "F.lean").write_text(
        "theorem foo : 1 = 1 := rfl\n\ntheorem bar : 2 = 2 := rfl\n", encoding="utf-8"
    )

    targets, capped = detect_changed_decls(tmp_path, base_sha, LEAN4)
    assert capped is False
    assert targets == [ChangedDecl(name="bar", file="F.lean", module="F")]


# ---------------------------------------------------------------------------
# Modified statement
# ---------------------------------------------------------------------------


def test_modified_statement_is_detected(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "F.lean").write_text("theorem foo : 1 = 1 := rfl\n", encoding="utf-8")
    base_sha = _commit_base(tmp_path)

    (tmp_path / "F.lean").write_text("theorem foo : 2 = 2 := rfl\n", encoding="utf-8")

    targets, _capped = detect_changed_decls(tmp_path, base_sha, LEAN4)
    assert targets == [ChangedDecl(name="foo", file="F.lean", module="F")]


def test_modified_proof_body_only_still_flags_when_stmt_extraction_agrees(
    tmp_path: Path,
) -> None:
    # Changing only the proof term (after `:=`) is NOT a statement change —
    # extract_statement stops at `:=`, so base/head statements match and
    # this declaration is correctly left untargeted.
    _init_repo(tmp_path)
    (tmp_path / "F.lean").write_text("theorem foo : True := trivial\n", encoding="utf-8")
    base_sha = _commit_base(tmp_path)

    (tmp_path / "F.lean").write_text(
        "theorem foo : True := by trivial\n", encoding="utf-8"
    )

    targets, _capped = detect_changed_decls(tmp_path, base_sha, LEAN4)
    assert targets == []


# ---------------------------------------------------------------------------
# Untouched declaration
# ---------------------------------------------------------------------------


def test_untouched_decl_not_targeted(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "F.lean").write_text(
        "theorem foo : 1 = 1 := rfl\ntheorem bar : 2 = 2 := rfl\n", encoding="utf-8"
    )
    base_sha = _commit_base(tmp_path)

    # Only bar changes; foo must not appear in the targets.
    (tmp_path / "F.lean").write_text(
        "theorem foo : 1 = 1 := rfl\ntheorem bar : 3 = 3 := rfl\n", encoding="utf-8"
    )

    targets, _capped = detect_changed_decls(tmp_path, base_sha, LEAN4)
    assert targets == [ChangedDecl(name="bar", file="F.lean", module="F")]


def test_no_changes_yields_no_targets(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "F.lean").write_text("theorem foo : 1 = 1 := rfl\n", encoding="utf-8")
    base_sha = _commit_base(tmp_path)

    targets, capped = detect_changed_decls(tmp_path, base_sha, LEAN4)
    assert targets == []
    assert capped is False


def test_declaration_renamed_in_place_is_detected_as_new_only(tmp_path: Path) -> None:
    # Renaming the declaration itself (same file, same statement body) is
    # distinct from a git-level file rename (see the module docstring's
    # git-mv note): the old name simply no longer appears in head_decls, so
    # it can't be flagged, and the new name has no base counterpart so it's
    # flagged as new. Regression guard against a crash or a stray old-name
    # entry leaking through.
    _init_repo(tmp_path)
    (tmp_path / "F.lean").write_text(
        "theorem old_name : True := trivial\n", encoding="utf-8"
    )
    base_sha = _commit_base(tmp_path)

    (tmp_path / "F.lean").write_text(
        "theorem new_name : True := trivial\n", encoding="utf-8"
    )

    targets, _capped = detect_changed_decls(tmp_path, base_sha, LEAN4)
    assert targets == [ChangedDecl(name="new_name", file="F.lean", module="F")]
    assert all(t.name != "old_name" for t in targets)


# ---------------------------------------------------------------------------
# Deleted file
# ---------------------------------------------------------------------------


def test_deleted_file_is_skipped(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "F.lean").write_text("theorem foo : 1 = 1 := rfl\n", encoding="utf-8")
    (tmp_path / "G.lean").write_text("theorem baz : 1 = 1 := rfl\n", encoding="utf-8")
    base_sha = _commit_base(tmp_path)

    (tmp_path / "G.lean").unlink()

    targets, _capped = detect_changed_decls(tmp_path, base_sha, LEAN4)
    assert targets == []


# ---------------------------------------------------------------------------
# Declarations with no `:=` — statement extractor returns None on both
# sides, so detection falls back to raw-line comparison.
# ---------------------------------------------------------------------------


def test_axiom_decl_change_detected_via_raw_line_fallback(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "F.lean").write_text("axiom bad : False\n", encoding="utf-8")
    base_sha = _commit_base(tmp_path)

    (tmp_path / "F.lean").write_text("axiom bad : True\n", encoding="utf-8")

    targets, _capped = detect_changed_decls(tmp_path, base_sha, LEAN4)
    assert targets == [ChangedDecl(name="bad", file="F.lean", module="F")]


def test_axiom_decl_unchanged_not_flagged_via_raw_line_fallback(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "F.lean").write_text(
        "axiom bad : False\naxiom other : False\n", encoding="utf-8"
    )
    base_sha = _commit_base(tmp_path)

    # Reformat unrelated whitespace elsewhere but leave `bad` untouched.
    (tmp_path / "F.lean").write_text(
        "axiom bad : False\n\naxiom other : True\n", encoding="utf-8"
    )

    targets, _capped = detect_changed_decls(tmp_path, base_sha, LEAN4)
    assert targets == [ChangedDecl(name="other", file="F.lean", module="F")]


# ---------------------------------------------------------------------------
# lean4 namespace qualification
# ---------------------------------------------------------------------------


def test_namespace_qualified_lean4_name(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "F.lean").write_text(
        textwrap.dedent(
            """\
            namespace Foo
            theorem bar : 1 = 1 := rfl
            end Foo
            """
        ),
        encoding="utf-8",
    )
    base_sha = _commit_base(tmp_path)

    (tmp_path / "F.lean").write_text(
        textwrap.dedent(
            """\
            namespace Foo
            theorem bar : 2 = 2 := rfl
            end Foo
            """
        ),
        encoding="utf-8",
    )

    targets, _capped = detect_changed_decls(tmp_path, base_sha, LEAN4)
    assert targets == [ChangedDecl(name="Foo.bar", file="F.lean", module="F")]


def test_nested_namespaces_qualification(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "F.lean").write_text(
        textwrap.dedent(
            """\
            namespace A
            namespace B
            theorem c : 1 = 1 := rfl
            end B
            end A
            """
        ),
        encoding="utf-8",
    )
    base_sha = _commit_base(tmp_path)

    (tmp_path / "F.lean").write_text(
        textwrap.dedent(
            """\
            namespace A
            namespace B
            theorem c : 2 = 2 := rfl
            end B
            end A
            """
        ),
        encoding="utf-8",
    )

    targets, _capped = detect_changed_decls(tmp_path, base_sha, LEAN4)
    assert targets == [ChangedDecl(name="A.B.c", file="F.lean", module="F")]


def test_bare_end_with_section_present_still_qualifies_correctly(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    src_base = textwrap.dedent(
        """\
        namespace Foo
        section
        theorem bar : 1 = 1 := rfl
        end
        theorem baz : 1 = 1 := rfl
        end Foo
        """
    )
    (tmp_path / "F.lean").write_text(src_base, encoding="utf-8")
    base_sha = _commit_base(tmp_path)

    src_head = src_base.replace("baz : 1 = 1 := rfl", "baz : 2 = 2 := rfl")
    (tmp_path / "F.lean").write_text(src_head, encoding="utf-8")

    targets, _capped = detect_changed_decls(tmp_path, base_sha, LEAN4)
    # `baz` is still inside `namespace Foo` (the bare `end` only closed
    # the anonymous `section`) so it must qualify as `Foo.baz`, not `baz`.
    assert targets == [ChangedDecl(name="Foo.baz", file="F.lean", module="F")]


def test_commented_namespace_does_not_requalify_declarations(tmp_path: Path) -> None:
    # F2 (statement-immutability hardening final round): `_extract` used
    # to feed `qualify_decl_names` the raw text, which tracks
    # `namespace`/`section`/`end` on a text-level stack — so a commented
    # -out `namespace Foo` pushed that stack and every declaration after
    # it qualified as `Foo.A.bar` instead of `A.bar`. Deleting the
    # (harmless, unrelated) comment while leaving `bar` itself untouched
    # then changed *only* base's qualified name, so `base_by_name` no
    # longer matched and the untouched declaration read as new.
    _init_repo(tmp_path)
    (tmp_path / "F.lean").write_text(
        "/-\nnamespace Foo\n-/\n"
        "namespace A\ntheorem bar : 1 = 1 := rfl\nend A\n",
        encoding="utf-8",
    )
    base_sha = _commit_base(tmp_path)

    # Worker tidies up the stray commented-out namespace opener; `bar`
    # itself is untouched.
    (tmp_path / "F.lean").write_text(
        "namespace A\ntheorem bar : 1 = 1 := rfl\nend A\n", encoding="utf-8"
    )

    targets, _capped = detect_changed_decls(tmp_path, base_sha, LEAN4)
    assert targets == []


def test_module_derivation_uses_full_relative_path(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "F.lean").write_text(
        "theorem foo : 1 = 1 := rfl\n", encoding="utf-8"
    )
    base_sha = _commit_base(tmp_path)

    (tmp_path / "sub" / "F.lean").write_text(
        "theorem foo : 1 = 1 := rfl\n\ntheorem bar : 2 = 2 := rfl\n", encoding="utf-8"
    )

    targets, _capped = detect_changed_decls(tmp_path, base_sha, LEAN4)
    assert targets == [ChangedDecl(name="bar", file="sub/F.lean", module="sub.F")]


# ---------------------------------------------------------------------------
# Deterministic ordering + cap
# ---------------------------------------------------------------------------


def test_ordering_is_by_file_then_line(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "B.lean").write_text("theorem old : True := trivial\n", encoding="utf-8")
    (tmp_path / "A.lean").write_text("theorem old : True := trivial\n", encoding="utf-8")
    base_sha = _commit_base(tmp_path)

    (tmp_path / "B.lean").write_text(
        "theorem old : True := trivial\ntheorem z : True := trivial\ntheorem a : True := trivial\n",
        encoding="utf-8",
    )
    (tmp_path / "A.lean").write_text(
        "theorem old : True := trivial\ntheorem m : True := trivial\n", encoding="utf-8"
    )

    targets, _capped = detect_changed_decls(tmp_path, base_sha, LEAN4)
    # File path order (A before B), then line number within each file.
    assert [(t.file, t.name) for t in targets] == [
        ("A.lean", "m"),
        ("B.lean", "z"),
        ("B.lean", "a"),
    ]


def test_cap_truncates_and_reports_capped(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "F.lean").write_text("-- empty\n", encoding="utf-8")
    base_sha = _commit_base(tmp_path)

    lines = [f"theorem t{i} : True := trivial" for i in range(5)]
    (tmp_path / "F.lean").write_text("\n".join(lines) + "\n", encoding="utf-8")

    targets, capped = detect_changed_decls(tmp_path, base_sha, LEAN4, cap=3)
    assert capped is True
    assert len(targets) == 3
    assert [t.name for t in targets] == ["t0", "t1", "t2"]


def test_cap_not_reported_when_under_limit(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "F.lean").write_text("-- empty\n", encoding="utf-8")
    base_sha = _commit_base(tmp_path)

    (tmp_path / "F.lean").write_text("theorem t : True := trivial\n", encoding="utf-8")

    targets, capped = detect_changed_decls(tmp_path, base_sha, LEAN4, cap=50)
    assert capped is False
    assert len(targets) == 1


# ---------------------------------------------------------------------------
# Non-matching extensions ignored
# ---------------------------------------------------------------------------


def test_non_matching_extension_files_ignored(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "F.lean").write_text("theorem foo : True := trivial\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# hello\n", encoding="utf-8")
    base_sha = _commit_base(tmp_path)

    (tmp_path / "README.md").write_text(
        "# hello\n\ntheorem sneaky : True := trivial\n", encoding="utf-8"
    )

    targets, _capped = detect_changed_decls(tmp_path, base_sha, LEAN4)
    assert targets == []


# ---------------------------------------------------------------------------
# Profile-generic: rocq surface names (no namespace qualification)
# ---------------------------------------------------------------------------


def test_rocq_uses_surface_names(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "F.v").write_text(
        "Theorem foo : True.\nProof. exact I. Qed.\n", encoding="utf-8"
    )
    base_sha = _commit_base(tmp_path)

    (tmp_path / "F.v").write_text(
        "Theorem foo : False.\nProof. Admitted.\n", encoding="utf-8"
    )

    targets, _capped = detect_changed_decls(tmp_path, base_sha, ROCQ)
    assert targets == [ChangedDecl(name="foo", file="F.v", module="F")]


# ---------------------------------------------------------------------------
# New file at head — every declaration counts as new.
# ---------------------------------------------------------------------------


def test_new_file_at_head_all_decls_are_new(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "README.md").write_text("# hello\n", encoding="utf-8")
    base_sha = _commit_base(tmp_path)

    # A real PR checkout has the new file already committed at head
    # (that's what "workspace's current checkout" means); a plain
    # `git diff <base_sha>` never sees a merely-untracked file.
    (tmp_path / "New.lean").write_text("theorem fresh : True := trivial\n", encoding="utf-8")
    _commit_base(tmp_path, "head: add New.lean")

    targets, _capped = detect_changed_decls(tmp_path, base_sha, LEAN4)
    assert targets == [ChangedDecl(name="fresh", file="New.lean", module="New")]


# ---------------------------------------------------------------------------
# Infrastructure error
# ---------------------------------------------------------------------------


def test_bad_base_sha_raises_changed_decls_error(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "F.lean").write_text("theorem foo : True := trivial\n", encoding="utf-8")
    _commit_base(tmp_path)

    with pytest.raises(ChangedDeclsError, match="git diff"):
        detect_changed_decls(tmp_path, "not-a-real-sha", LEAN4)


def test_not_a_git_repo_raises_changed_decls_error(tmp_path: Path) -> None:
    (tmp_path / "F.lean").write_text("theorem foo : True := trivial\n", encoding="utf-8")
    with pytest.raises(ChangedDeclsError):
        detect_changed_decls(tmp_path, "HEAD", LEAN4)


def test_rocq_probe_targets_are_not_scope_path_qualified() -> None:
    """Round 4 (F2) gave isabelle and rocq a `qualify_decl_names` hook,
    and `changed_decls` must keep ignoring it.

    What those two profiles supply is a *disambiguation key* — an
    enclosing-scope path, deliberately not the name the prover resolves
    (`gate.provers.decl_syntax.qualify_by_scope`). It is safe where a
    key is only ever compared against another key computed the same
    way. Here the names become trust-report probe targets the prover
    itself must resolve (`Print Assumptions <name>.`), so a path that
    ignores `Include` or functor application would not resolve at all.
    `_extract`'s `profile.name == "lean4"` gate is what prevents that,
    and this pins it: a `Theorem` inside a `Module` must be probed as
    `c`, never as `A.c`."""
    text = (
        "Module A.\n"
        "Theorem c : 1 = 1.\n"
        "Proof. reflexivity. Qed.\n"
        "End A.\n"
    )
    names = [d.name for d in changed_decls._extract(text, "F.v", ROCQ)]
    assert names == ["c"]


def test_isabelle_probe_targets_are_not_scope_path_qualified() -> None:
    """The isabelle half of the same pin — see above.

    `A` is in the list because round 7 (F2) made `locale` an enumerated
    declaration, and this module targets whatever enumeration reports.
    The pin's own subject is unaffected: `c`, not `A.c`.

    `A` is not a fact, so the probe reports it `unresolved` — and that
    is a pre-existing property of isabelle probing rather than something
    F2 introduced. The ML resolves targets with
    `Proof_Context.get_thm`, so every `definition` / `consts` /
    `typedecl` / `datatype` / `record` / `type_synonym` name in a diff
    already fails the same way (a `definition f`'s fact is `f_def`, not
    `f`). Narrowing isabelle probe targets to fact-bearing kinds is a
    real improvement and a separate one — round 5's `_is_probeable_name`
    filters by identifier SHAPE, which cannot distinguish these, so it
    needs a per-kind signal this module does not have today.
    """
    text = "locale A begin\nlemma c: \"x = x\"\nsorry\nend\n"
    names = [d.name for d in changed_decls._extract(text, "F.thy", ISABELLE)]
    assert names == ["A", "c"]


# ---------------------------------------------------------------------------
# Probeable-name filtering (round 5, F1)
# ---------------------------------------------------------------------------


def test_anonymous_lean4_decls_enumerate_as_colon() -> None:
    """The premise of F1, pinned so it cannot silently change.

    Enumeration is *right* to report these — the statement-immutability
    audit compares them by key — but the key is the literal `:`, which
    is why the probe consumer has to filter rather than enumeration
    narrowing (`gate.provers.decl_syntax.normalize_decl_name`).
    """
    text = (
        "example : True := trivial\n"
        "theorem real : True := trivial\n"
        "instance : Inhabited Nat := ⟨0⟩\n"
    )
    names = [d.name for d in changed_decls._extract(text, "F.lean", LEAN4)]
    assert names == [":", "real", ":"]


def test_anonymous_decls_are_not_probe_targets(tmp_path: Path) -> None:
    """`#print axioms :` is an elaboration error, so `:` is never a target.

    Verified against a live v4.32.0 toolchain before the fix: each such
    target produced one `unresolved` report line (the per-target probe
    isolates the failure), one wasted `lake env lean` invocation, and
    one consumed `cap` slot.
    """
    _init_repo(tmp_path)
    (tmp_path / "F.lean").write_text(
        "theorem keep : 1 = 1 := rfl\n", encoding="utf-8"
    )
    base_sha = _commit_base(tmp_path)

    (tmp_path / "F.lean").write_text(
        "theorem keep : 1 = 1 := rfl\n"
        "example : True := trivial\n"
        "instance : Inhabited Nat := ⟨0⟩\n"
        "theorem added : 2 = 2 := rfl\n",
        encoding="utf-8",
    )
    targets, capped = detect_changed_decls(tmp_path, base_sha, LEAN4)

    assert [t.name for t in targets] == ["added"]
    assert capped is False


def test_binder_first_header_is_not_a_probe_target(tmp_path: Path) -> None:
    """Not just `:` — a binder-first anonymous header enumerates as `{α`.

    This is the case a `name != ":"` blacklist would have missed.
    """
    _init_repo(tmp_path)
    (tmp_path / "F.lean").write_text("theorem keep : 1 = 1 := rfl\n", encoding="utf-8")
    base_sha = _commit_base(tmp_path)

    (tmp_path / "F.lean").write_text(
        "theorem keep : 1 = 1 := rfl\n"
        "example {α : Type} (x : α) : x = x := rfl\n",
        encoding="utf-8",
    )
    targets, _capped = detect_changed_decls(tmp_path, base_sha, LEAN4)

    assert targets == []


def test_isabelle_parameterized_datatype_now_enumerates_under_its_name(
    tmp_path: Path,
) -> None:
    r"""Round 11 F3 changed this, and the change is worth stating in full.

    `datatype 'a tree = …` used to enumerate as `'a`, because
    `decl_line_regex`'s `(\S+)` captured the type parameter — so this
    test asserted the name was unprobeable. It now enumerates as `tree`
    via `ProverProfile.decl_type_params_syntax`, which is what the
    statement-immutability audit needs (every same-keyword type
    declaration in one theory previously collided under a key like `'a`;
    1 725 of 1 879 measured isabelle duplicate-name false blocks were
    keyed on a non-identifier).

    The cost lands here rather than there: `tree` is a *type*
    constructor, not a theorem, so it becomes a trust-report probe
    target that Isabelle cannot resolve. That is exactly the per-target
    cost this module's docstring already documents for anonymous
    declarations — `collect_trust_report` probes one declaration at a
    time, so it is one `unresolved` report line and one consumed cap
    slot, never a failed run. Filtering it out would need the
    declaration's *keyword*, which `extract_declarations` does not
    carry.
    """
    _init_repo(tmp_path)
    (tmp_path / "F.thy").write_text("theory F begin\nend\n", encoding="utf-8")
    base_sha = _commit_base(tmp_path)

    (tmp_path / "F.thy").write_text(
        "theory F begin\ndatatype 'a tree = Leaf | Node 'a\nend\n", encoding="utf-8"
    )
    targets, _capped = detect_changed_decls(tmp_path, base_sha, ISABELLE)

    assert [t.name for t in targets] == ["tree"]


def test_probeable_name_shapes() -> None:
    """The identifier shapes the filter accepts and rejects."""
    accepted = [
        "foo",
        "Nat.add_comm",
        "add_comm'",
        "h₁",
        "_root_.foo",
        "A.c",
        "decide?",
        "Foo.bar!",
        "α",
    ]
    rejected = [":", ":=", "{α", "(n", "'a", "('a", "foo.", ".foo", "1foo", "foo:bar"]
    for name in accepted:
        assert changed_decls._is_probeable_name(name), name
    for name in rejected:
        assert not changed_decls._is_probeable_name(name), name


def test_repeated_surface_name_is_targeted_rather_than_mis_compared(
    tmp_path: Path,
) -> None:
    """A name repeated on either side is probed, not compared (round 5, F1).

    `extract_statement` resolves a name to its first match, so with two
    `Theorem c`s in sibling rocq `Module`s the old single-entry
    `base_by_name` compared every `c` against one arbitrary pair — and
    a change to the *second* one read as no change at all.
    """
    _init_repo(tmp_path)
    (tmp_path / "F.v").write_text(
        "Module A.\n"
        "Theorem c : 1 = 1.\n"
        "Proof. reflexivity. Qed.\n"
        "End A.\n"
        "Module B.\n"
        "Theorem c : 2 = 2.\n"
        "Proof. reflexivity. Qed.\n"
        "End B.\n",
        encoding="utf-8",
    )
    base_sha = _commit_base(tmp_path)

    # Retype only B's statement. A's is untouched and comes first, so a
    # first-match comparison sees nothing.
    (tmp_path / "F.v").write_text(
        "Module A.\n"
        "Theorem c : 1 = 1.\n"
        "Proof. reflexivity. Qed.\n"
        "End A.\n"
        "Module B.\n"
        "Theorem c : 3 = 3.\n"
        "Proof. reflexivity. Qed.\n"
        "End B.\n",
        encoding="utf-8",
    )
    targets, _capped = detect_changed_decls(tmp_path, base_sha, ROCQ)

    assert [t.name for t in targets] == ["c", "c"]
