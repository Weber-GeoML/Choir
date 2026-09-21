"""Tests for the pure helpers in `client.workspace`.

The I/O parts (`setup_workspace` itself) are covered by the demo
integration run; mocking gh + git here would be a lot of plumbing for
little additional confidence.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import client.workspace as wsmod
from client import repo_store
from client.repo_store import RepoStoreError
from client.workspace import (
    WORKSPACE_EXCLUDES,
    LeaseMetadata,
    add_local_excludes,
    assemble_skill_context,
    branch_name,
    build_choir_md,
    find_workspace_root,
    slugify_decl,
    sync_skills,
    task_slug,
    workspace_path,
    workspace_profile,
    workspace_root,
    write_choir_md,
)
from gate.provers import ProverError, get_profile
from gate.provers.lean4 import LEAN4
from gate.state.task_record import ProjectRef, TaskRecord, TaskType

# ---------------------------------------------------------------------------
# slugify_decl
# ---------------------------------------------------------------------------


def test_slugify_decl_folds_case_dots_and_apostrophes() -> None:
    # Apostrophes (allowed in target_decl per design note 02) become hyphens,
    # runs of separators collapse, and edges are trimmed.
    assert slugify_decl("SampleProject.one_add_one") == "sampleproject-one-add-one"
    assert slugify_decl("a..b___c") == "a-b-c"
    assert slugify_decl("...foo_bar...") == "foo-bar"
    assert slugify_decl("Mathlib.Foo.add_comm'") == "mathlib-foo-add-comm"


# ---------------------------------------------------------------------------
# branch_name
# ---------------------------------------------------------------------------


def test_branch_name_is_the_issue_number_and_the_slug() -> None:
    assert branch_name(42, "SampleProject.one_add_one") == "choir/42-sampleproject-one-add-one"


# ---------------------------------------------------------------------------
# workspace_path / workspace_root
# ---------------------------------------------------------------------------


def test_workspace_root_uses_env_override(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("CHOIR_WORK_ROOT", str(tmp_path))
    assert workspace_root() == tmp_path


def test_workspace_path_under_root(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("CHOIR_WORK_ROOT", str(tmp_path))
    p = workspace_path("alice/proj", 42)
    assert p == tmp_path / "alice" / "proj" / "42"


def test_workspace_root_falls_back_to_home_choir(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("CHOIR_WORK_ROOT", raising=False)
    assert workspace_root().parts[-2:] == (".choir", "work")


# ---------------------------------------------------------------------------
# LeaseMetadata round-trip
# ---------------------------------------------------------------------------


def _sample_meta() -> LeaseMetadata:
    return LeaseMetadata(
        repo="alice/proj",
        issue=42,
        branch="choir/42-add-comm",
        pinned_commit="1a2b3c4d",
        claimed_at="2026-05-10T15:32:00+00:00",
        claimed_by="alice",
        target_file="MyProj/Foo.lean",
        target_decl="MyProj.Foo.add_comm",
        task_type="prove",
    )


def test_lease_metadata_write_read_roundtrip(tmp_path: Path) -> None:
    meta = _sample_meta()
    path = tmp_path / ".choir-lease.json"
    meta.write(path)
    loaded = LeaseMetadata.read(path)
    assert loaded == meta


def test_lease_metadata_file_is_json(tmp_path: Path) -> None:
    meta = _sample_meta()
    path = tmp_path / ".choir-lease.json"
    meta.write(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["repo"] == "alice/proj"
    assert raw["issue"] == 42


def test_lease_metadata_read_fails_loudly_on_missing_field(tmp_path: Path) -> None:
    path = tmp_path / ".choir-lease.json"
    path.write_text('{"repo": "alice/proj"}\n', encoding="utf-8")
    with pytest.raises(TypeError):
        LeaseMetadata.read(path)


def test_lease_metadata_carries_skills_commit(tmp_path: Path) -> None:
    meta = _sample_meta()
    meta.skills_commit = "abc1234"
    path = tmp_path / ".choir-lease.json"
    meta.write(path)
    assert LeaseMetadata.read(path).skills_commit == "abc1234"


def test_lease_metadata_reads_legacy_file_without_skills_commit(tmp_path: Path) -> None:
    # A lease written before skills_commit existed must still load.
    path = tmp_path / ".choir-lease.json"
    path.write_text(json.dumps({
        "repo": "alice/proj", "issue": 42, "branch": "choir/42-x",
        "pinned_commit": "1a2b3c4d", "claimed_at": "2026-05-10T15:32:00+00:00",
        "claimed_by": "alice", "target_file": "P/Foo.lean",
        "target_decl": "P.Foo.add_comm", "task_type": "prove",
    }) + "\n", encoding="utf-8")
    assert LeaseMetadata.read(path).skills_commit == ""


def test_lease_metadata_reads_file_with_unknown_extra_key(tmp_path: Path) -> None:
    # A lease written by an OLDER client whose LeaseMetadata carried a
    # field this version dropped (e.g. `target_pr`, retired by spec D1)
    # must still load rather than raising `TypeError: unexpected keyword
    # argument` — the missing-key direction is covered above; this is the
    # extra-key direction.
    path = tmp_path / ".choir-lease.json"
    path.write_text(json.dumps({
        "repo": "alice/proj", "issue": 42, "branch": "choir/42-x",
        "pinned_commit": "1a2b3c4d", "claimed_at": "2026-05-10T15:32:00+00:00",
        "claimed_by": "alice", "target_file": "P/Foo.lean",
        "target_decl": "P.Foo.add_comm", "task_type": "prove",
        "skills_commit": "", "target_pr": 42, "some_future_field": "whatever",
    }) + "\n", encoding="utf-8")
    meta = LeaseMetadata.read(path)
    assert meta.repo == "alice/proj"
    assert not hasattr(meta, "target_pr")


# ---------------------------------------------------------------------------
# find_workspace_root
# ---------------------------------------------------------------------------


def test_find_workspace_root_in_current_dir(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("CHOIR_WORK_ROOT", str(tmp_path))
    (tmp_path / ".choir-lease.json").write_text("{}", encoding="utf-8")
    assert find_workspace_root(tmp_path) == tmp_path


def test_find_workspace_root_in_ancestor(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("CHOIR_WORK_ROOT", str(tmp_path))
    (tmp_path / ".choir-lease.json").write_text("{}", encoding="utf-8")
    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)
    assert find_workspace_root(deep) == tmp_path


def test_find_workspace_root_returns_none_outside_any_workspace(
    monkeypatch, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("CHOIR_WORK_ROOT", str(tmp_path))
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)
    assert find_workspace_root(deep) is None


# ---------------------------------------------------------------------------
# Bug #8 regression: find_workspace_root won't escape workspace_root()
# ---------------------------------------------------------------------------


def test_find_workspace_root_ignores_stray_lease_outside_work_root(
    monkeypatch, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    # Setup: workspace root is tmp_path/.choir/work. A stray
    # `.choir-lease.json` sits at tmp_path itself (outside the work
    # root). Walking up from anywhere under the work root must NOT
    # pick up the stray file.
    work_root = tmp_path / ".choir" / "work"
    work_root.mkdir(parents=True)
    monkeypatch.setenv("CHOIR_WORK_ROOT", str(work_root))
    # Stray lease in an ancestor — must be ignored.
    (tmp_path / ".choir-lease.json").write_text("{}", encoding="utf-8")
    # Search starts inside the work root but no legitimate lease present.
    deep = work_root / "alice" / "proj" / "42"
    deep.mkdir(parents=True)
    assert find_workspace_root(deep) is None


def test_find_workspace_root_returns_none_when_start_is_outside_work_root(
    monkeypatch, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    # If the caller is in /tmp (not in the work root), find_workspace_root
    # returns None even if a stray .choir-lease.json sits in /tmp.
    work_root = tmp_path / "work"
    work_root.mkdir()
    monkeypatch.setenv("CHOIR_WORK_ROOT", str(work_root))
    stray = tmp_path / "elsewhere"
    stray.mkdir()
    (stray / ".choir-lease.json").write_text("{}", encoding="utf-8")
    assert find_workspace_root(stray) is None


# ---------------------------------------------------------------------------
# CHOIR.md primer
# ---------------------------------------------------------------------------


def test_write_choir_md_creates_file(tmp_path: Path) -> None:
    write_choir_md(tmp_path, LEAN4)
    assert (tmp_path / "CHOIR.md").is_file()



def test_choir_md_is_overwriteable(tmp_path: Path) -> None:
    # Calling twice should leave the canonical content (not append).
    (tmp_path / "CHOIR.md").write_text("garbage", encoding="utf-8")
    write_choir_md(tmp_path, LEAN4)
    content = (tmp_path / "CHOIR.md").read_text(encoding="utf-8")
    assert "garbage" not in content
    assert content == build_choir_md(LEAN4)



# ---------------------------------------------------------------------------
# build_choir_md: profile-injected facts (Task 6 / design note 12 §6)
# ---------------------------------------------------------------------------



def test_build_choir_md_rocq_mentions_rocq_facts_not_lean() -> None:
    content = build_choir_md(get_profile("rocq"))
    assert "dune build" in content
    assert "Admitted" in content
    assert "lake " not in content



def test_build_choir_md_protected_files_include_substrate_set() -> None:
    # Every prover protects the substrate paths, plus its own files.
    content = build_choir_md(get_profile("rocq"))
    assert ".choir/" in content
    assert ".github/" in content
    assert "skills/" in content
    assert "_CoqProject" in content


# ---------------------------------------------------------------------------
# workspace_profile: resolve-and-fallback helper (Task 6)
# ---------------------------------------------------------------------------


def test_workspace_profile_defaults_to_lean4_when_no_project_toml(tmp_path: Path) -> None:
    assert workspace_profile(tmp_path).name == "lean4"


def test_workspace_profile_reads_selected_prover(tmp_path: Path) -> None:
    choir_dir = tmp_path / ".choir"
    choir_dir.mkdir()
    (choir_dir / "project.toml").write_text('[project]\nprover = "rocq"\n', encoding="utf-8")
    assert workspace_profile(tmp_path).name == "rocq"


def test_workspace_profile_falls_back_to_lean4_on_malformed_toml(
    tmp_path: Path, capsys
) -> None:  # type: ignore[no-untyped-def]
    choir_dir = tmp_path / ".choir"
    choir_dir.mkdir()
    (choir_dir / "project.toml").write_text("this is not [ valid toml", encoding="utf-8")

    profile = workspace_profile(tmp_path)

    assert profile is LEAN4
    captured = capsys.readouterr()
    assert "warning" in captured.out.lower()


def test_workspace_profile_falls_back_to_lean4_on_unknown_prover(
    tmp_path: Path, capsys
) -> None:  # type: ignore[no-untyped-def]
    choir_dir = tmp_path / ".choir"
    choir_dir.mkdir()
    (choir_dir / "project.toml").write_text(
        '[project]\nprover = "not-a-real-prover"\n', encoding="utf-8"
    )

    profile = workspace_profile(tmp_path)

    assert profile is LEAN4
    captured = capsys.readouterr()
    assert "warning" in captured.out.lower()


def test_workspace_profile_propagates_read_prover_error_type(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    # Any ProverError from the select module is caught, not re-raised.
    def boom(path):  # type: ignore[no-untyped-def]
        raise ProverError("simulated")

    monkeypatch.setattr(wsmod, "read_prover", boom)
    assert workspace_profile(tmp_path).name == "lean4"


# ---------------------------------------------------------------------------
# add_local_excludes
# ---------------------------------------------------------------------------


def test_add_local_excludes_appends(tmp_path: Path) -> None:
    info = tmp_path / ".git" / "info"
    info.mkdir(parents=True)
    (info / "exclude").write_text("# existing\n", encoding="utf-8")
    add_local_excludes(tmp_path, [".choir-skills/", ".choir-context.md"])
    text = (info / "exclude").read_text(encoding="utf-8")
    assert "# existing" in text
    assert ".choir-skills/" in text
    assert ".choir-context.md" in text


def test_add_local_excludes_idempotent(tmp_path: Path) -> None:
    info = tmp_path / ".git" / "info"
    info.mkdir(parents=True)
    (info / "exclude").write_text("", encoding="utf-8")
    add_local_excludes(tmp_path, [".choir-skills/"])
    add_local_excludes(tmp_path, [".choir-skills/"])
    text = (info / "exclude").read_text(encoding="utf-8")
    assert text.splitlines().count(".choir-skills/") == 1


def test_add_local_excludes_no_git_info_is_noop(tmp_path: Path) -> None:
    # No .git/info — must not raise.
    add_local_excludes(tmp_path, [".choir-skills/"])


# ---------------------------------------------------------------------------
# sync_skills
# ---------------------------------------------------------------------------


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _make_origin_with_skills(origin: Path, *, versions: list[tuple[str, str]]) -> None:
    """Init a repo at `origin` and commit skills/conventions.md once per (content, msg)."""
    origin.mkdir()
    _git(["init", "-b", "main"], origin)
    _git(["config", "user.email", "t@t"], origin)
    _git(["config", "user.name", "t"], origin)
    (origin / "skills").mkdir()
    for content, msg in versions:
        (origin / "skills" / "conventions.md").write_text(content, encoding="utf-8")
        _git(["add", "-A"], origin)
        _git(["commit", "-m", msg], origin)


def test_sync_skills_extracts_latest_not_pinned(tmp_path: Path) -> None:
    origin = tmp_path / "origin"
    _make_origin_with_skills(origin, versions=[("v1-old", "v1"), ("v2-latest", "v2")])
    ws = tmp_path / "ws"
    subprocess.run(["git", "clone", str(origin), str(ws)], check=True, capture_output=True)

    sha = sync_skills(ws)

    assert sha and len(sha) >= 7
    assert (ws / ".choir-skills" / "conventions.md").read_text(encoding="utf-8") == "v2-latest"


def test_sync_skills_no_pack_returns_sha_empty_dir(tmp_path: Path) -> None:
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(["init", "-b", "main"], origin)
    _git(["config", "user.email", "t@t"], origin)
    _git(["config", "user.name", "t"], origin)
    (origin / "README.md").write_text("no skills here", encoding="utf-8")
    _git(["add", "-A"], origin)
    _git(["commit", "-m", "init"], origin)
    ws = tmp_path / "ws"
    subprocess.run(["git", "clone", str(origin), str(ws)], check=True, capture_output=True)

    sha = sync_skills(ws)

    assert sha  # we still record what we synced against
    assert list((ws / ".choir-skills").iterdir()) == []


# ---------------------------------------------------------------------------
# assemble_skill_context
# ---------------------------------------------------------------------------


def test_assemble_skill_context_concatenates(tmp_path: Path) -> None:
    sk = tmp_path / ".choir-skills"
    sk.mkdir()
    (sk / "conventions.md").write_text("Use snake_case", encoding="utf-8")
    (sk / "tactics.md").write_text("Prefer simp", encoding="utf-8")
    out = assemble_skill_context(tmp_path)
    assert out == tmp_path / ".choir-context.md"
    text = out.read_text(encoding="utf-8")
    assert "Use snake_case" in text
    assert "Prefer simp" in text
    assert "TASK.md" in text  # header points the agent at its task


def test_assemble_skill_context_none_when_no_skills(tmp_path: Path) -> None:
    assert assemble_skill_context(tmp_path) is None       # no .choir-skills dir
    (tmp_path / ".choir-skills").mkdir()
    assert assemble_skill_context(tmp_path) is None       # dir present but empty


# ---------------------------------------------------------------------------
# CHOIR.md primer points at .choir-context.md (Task 5)
# ---------------------------------------------------------------------------


def test_choir_md_points_at_synced_context(tmp_path: Path) -> None:
    write_choir_md(tmp_path, LEAN4)
    content = (tmp_path / "CHOIR.md").read_text(encoding="utf-8")
    assert ".choir-context.md" in content
    # And it still documents the protected skills/ path (unchanged contract).
    assert "skills/" in content


# ---------------------------------------------------------------------------
# add_local_excludes in a worktree (note 09 §1 — .git is a file, not a dir)
# ---------------------------------------------------------------------------


def test_add_local_excludes_works_in_a_worktree(tmp_path: Path) -> None:
    # Build a tiny repo + worktree; the exclude must land in the common gitdir.
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(["init", "-b", "main"], origin)
    _git(["config", "user.email", "t@t"], origin)
    _git(["config", "user.name", "t"], origin)
    (origin / "f.txt").write_text("x", encoding="utf-8")
    _git(["add", "-A"], origin)
    _git(["commit", "-m", "init"], origin)
    wt = tmp_path / "wt"
    _git(["worktree", "add", "-b", "feat", str(wt)], origin)
    assert (wt / ".git").is_file()  # confirm it's really a worktree

    add_local_excludes(wt, [".choir-skills/", ".choir-context.md"])

    exclude = origin / ".git" / "info" / "exclude"
    text = exclude.read_text(encoding="utf-8")
    assert ".choir-skills/" in text
    assert ".choir-context.md" in text


def test_choir_own_files_do_not_read_as_dirty(tmp_path: Path) -> None:
    """`choir purge` guards on `git status --porcelain`, so the files Choir
    itself writes into a workspace must never appear there."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for name in ("TASK.md", "CHOIR.md", ".choir-lease.json", ".choir-context.md"):
        (tmp_path / name).write_text("x", encoding="utf-8")
    (tmp_path / ".choir-skills").mkdir()
    (tmp_path / ".choir-skills" / "primer.md").write_text("x", encoding="utf-8")

    add_local_excludes(tmp_path, WORKSPACE_EXCLUDES)

    status = subprocess.run(
        ["git", "-C", str(tmp_path), "status", "--porcelain"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert status == ""


# ---------------------------------------------------------------------------
# setup_workspace wiring: worktree path + fallback (note 09 §1)
# ---------------------------------------------------------------------------


def _task_record(commit: str = "c0ffee5") -> TaskRecord:
    return TaskRecord(
        choir_task_version=1,
        type=TaskType.PROVE,
        target_file="Proj.lean",
        target_decl="Proj.t",
        project_ref=ProjectRef(
            repo="alice/proj", commit=commit, toolchain="leanprover/lean4:v4.12.0"
        ),
        deps=[],
    )


def test_setup_workspace_calls_create_worktree(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("CHOIR_WORK_ROOT", str(tmp_path / "work"))
    seen = {}

    def fake_create_worktree(repo, *, workdir, branch, commit, **kw):  # type: ignore[no-untyped-def]
        seen.update(repo=repo, branch=branch, commit=commit)
        workdir.mkdir(parents=True)
        (workdir / ".git").write_text("gitdir: /fake\n", encoding="utf-8")  # worktree marker
        return tmp_path / "store"

    monkeypatch.setattr(wsmod.repo_store, "create_worktree", fake_create_worktree)
    monkeypatch.setattr(wsmod, "sync_skills", lambda ws: "deadbeef")
    monkeypatch.setattr(wsmod, "assemble_skill_context", lambda ws: None)
    monkeypatch.setattr(wsmod, "add_local_excludes", lambda ws, names: None)

    path = wsmod.setup_workspace(
        repo="alice/proj",
        issue=42,
        record=_task_record(),
        body_prose="do it",
        claimed_by="bob",
    )
    assert seen == {"repo": "alice/proj", "branch": "choir/42-proj-t", "commit": "c0ffee5"}
    assert (path / "TASK.md").read_text(encoding="utf-8").startswith("do it")
    assert LeaseMetadata.read(path / ".choir-lease.json").pinned_commit == "c0ffee5"


def test_setup_workspace_falls_back_to_clone_on_store_error(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("CHOIR_WORK_ROOT", str(tmp_path / "work"))
    calls = {"clone": 0}

    def boom(*a, **k):  # type: ignore[no-untyped-def]
        raise RepoStoreError("no worktree today")

    def fake_clone_fallback(repo, issue, record, path):  # type: ignore[no-untyped-def]
        calls["clone"] += 1
        path.mkdir(parents=True)

    monkeypatch.setattr(wsmod.repo_store, "create_worktree", boom)
    monkeypatch.setattr(wsmod, "_legacy_clone_checkout", fake_clone_fallback)
    monkeypatch.setattr(wsmod, "sync_skills", lambda ws: "")
    monkeypatch.setattr(wsmod, "assemble_skill_context", lambda ws: None)
    monkeypatch.setattr(wsmod, "add_local_excludes", lambda ws, names: None)

    wsmod.setup_workspace(
        repo="alice/proj",
        issue=7,
        record=_task_record("abc1234"),
        body_prose="x",
        claimed_by="bob",
    )
    assert calls["clone"] == 1


# ---------------------------------------------------------------------------
# Integration: real create_worktree + real sync_skills (note 09 §1 — the
# claim that skill sync works unchanged in a --no-checkout worktree, since
# origin/HEAD must resolve there or sync_skills silently no-ops)
# ---------------------------------------------------------------------------


def test_sync_skills_works_in_a_worktree(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("CHOIR_REPO_STORE", str(tmp_path / "rs"))
    origin = tmp_path / "origin"
    _make_origin_with_skills(origin, versions=[("v1-old", "v1"), ("v2-latest", "v2")])
    sha = subprocess.run(
        ["git", "-C", str(origin), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    def local_clone(repo: str, dest: Path) -> None:
        subprocess.run(
            ["git", "clone", "--no-checkout", repo, str(dest)],
            check=True,
            capture_output=True,
        )

    wt = tmp_path / "wt"
    repo_store.create_worktree(
        str(origin), workdir=wt, branch="choir/1-x", commit=sha, clone=local_clone
    )

    synced = sync_skills(wt)
    assert synced and len(synced) >= 7  # origin/HEAD resolved → real sha, not None
    assert (wt / ".choir-skills" / "conventions.md").read_text(encoding="utf-8") == "v2-latest"
    assert assemble_skill_context(wt) is not None


# ---------------------------------------------------------------------------
# task_slug
# ---------------------------------------------------------------------------


class TestTaskSlug:
    def _record(self, **kw):
        base = {
            "choir-task-version": 1,
            "project_ref": ProjectRef(
                repo="acme/proofs", commit="a" * 40,
                toolchain="leanprover/lean4:v4.31.0",
            ),
            "deps": [],
        }
        return TaskRecord(**{**base, **kw})

    def test_prove_slug_unchanged(self):
        rec = self._record(type="prove", target_file="Foo.lean",
                            target_decl="SampleProject.one_add_one")
        assert task_slug(rec) == "sampleproject-one-add-one"

