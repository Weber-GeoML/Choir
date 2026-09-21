"""Workspace setup for a claimed Choir task.

After a successful claim, the contributor CLI clones the project repo
into a per-issue workspace, checks out the pinned commit, creates the
topic branch, and drops the issue body verbatim as `TASK.md`. The
contributor (or their agent) does the work in this workspace; `choir
submit` reads the lease metadata to push and open the PR.

Workspace layout::

    ~/.choir/work/<owner>/<repo>/<issue>/
    ├── .git/
    ├── .choir-lease.json     # metadata read by `choir submit`
    ├── TASK.md               # issue prose verbatim
    └── ...                   # project tree at the pinned commit

The root directory can be overridden via `$CHOIR_WORK_ROOT` (mostly for
tests; the default is fine in production).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime
from pathlib import Path

from client import repo_store
from gate.provers import ProverError, ProverProfile, get_profile
from gate.provers.lean4 import LEAN4
from gate.provers.select import read_prover
from gate.state.task_record import TaskRecord

# Files Choir writes into a workspace that belong to the task, not the project.
# Excluded locally (never committed) so `git status` in a workspace reflects the
# contributor's own edits: `choir purge` guards on a clean tree, and an agent
# running `git add -A` must not sweep them into a PR.
WORKSPACE_EXCLUDES = [
    ".choir-skills/",
    ".choir-context.md",
    "TASK.md",
    "CHOIR.md",
    ".choir-lease.json",
]


class WorkspaceError(RuntimeError):
    """A workspace operation failed (clone, checkout, branch, or metadata write)."""


@dataclass
class LeaseMetadata:
    """On-disk record of an active lease. Lives at `.choir-lease.json` in the workspace."""

    repo: str
    issue: int
    branch: str
    pinned_commit: str
    claimed_at: str
    claimed_by: str
    target_file: str
    target_decl: str
    task_type: str
    skills_commit: str = ""  # default-branch SHA skills were synced from (design note 06)
    # The worker session that won this lease. One login can run several, so
    # `heartbeat` and any later re-claim must present the id the claim was
    # made under — a fresh one reads as a different session and would lose
    # the race to itself. Empty for a workspace written before the field.
    session: str = ""

    def write(self, path: Path) -> None:
        path.write_text(
            json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8"
        )

    @classmethod
    def read(cls, path: Path) -> LeaseMetadata:
        """Read a lease file, tolerating unknown keys from an older/newer client.

        Filters to this dataclass's own field names before constructing —
        a lease file written by a client whose `LeaseMetadata` carried a
        field this version no longer has (e.g. the `target_pr` retired by
        spec D1) must still load, not raise `TypeError: unexpected keyword
        argument`. Fields this version requires but the file lacks still
        raise loudly (`cls(**filtered)` has no defaults for them) — this
        only widens tolerance in the extra-key direction, not the
        missing-key one.
        """
        data = json.loads(path.read_text(encoding="utf-8"))
        known = {f.name for f in fields(cls)}
        filtered = {k: v for k, v in data.items() if k in known}
        return cls(**filtered)


def workspace_root() -> Path:
    """Root for all Choir workspaces. `$CHOIR_WORK_ROOT` overrides the default."""
    override = os.environ.get("CHOIR_WORK_ROOT")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".choir" / "work"


def workspace_path(repo: str, issue: int) -> Path:
    return workspace_root() / repo / str(issue)


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify_decl(decl: str) -> str:
    """Turn a fully-qualified Lean name into a branch-safe slug.

    `SampleProject.one_add_one` -> `sampleproject-one-add-one`.
    """
    s = _SLUG_RE.sub("-", decl.lower())
    return s.strip("-")


def branch_name(issue: int, decl: str) -> str:
    """Choir-namespaced topic branch for a given issue + target_decl."""
    return f"choir/{issue}-{slugify_decl(decl)}"


def task_slug(record: TaskRecord) -> str:
    """Branch-safe slug from the task's target declaration."""
    return slugify_decl(record.target_decl or "")


def find_workspace_root(start: Path) -> Path | None:
    """Walk up from `start`, return the first directory containing `.choir-lease.json`.

    Restricted to paths *under* `workspace_root()`. A stray
    `.choir-lease.json` outside the Choir work directory (e.g. in
    `/tmp`, in `$HOME`, in any ancestor of the user's cwd that isn't
    under `~/.choir/work`) is ignored — it's not a Choir workspace,
    and honoring it would let a malicious or stale file redirect
    `choir submit` / `choir release` toward an unintended issue.

    Returns None if `start` isn't under `workspace_root()` at all,
    OR if no `.choir-lease.json` is found in the in-scope ancestors.
    """
    try:
        cap = workspace_root().resolve()
        start_resolved = start.resolve()
    except OSError:
        return None

    # `start` must be under the workspace root, otherwise no lookup
    # is valid. `is_relative_to` is the 3.9+ pathlib method.
    if not start_resolved.is_relative_to(cap):
        return None

    for path in [start_resolved, *start_resolved.parents]:
        if (path / ".choir-lease.json").is_file():
            return path
        if path == cap:
            break
    return None


def setup_workspace(
    *,
    repo: str,
    issue: int,
    record: TaskRecord,
    body_prose: str,
    claimed_by: str,
    session: str = "",
) -> Path:
    """Clone, check out the pinned commit on a topic branch, write metadata.

    Returns the workspace path. Raises `WorkspaceError` on any failure.
    """
    path = workspace_path(repo, issue)
    if path.exists():
        raise WorkspaceError(
            f"workspace already exists at {path} — remove it or release the lease first"
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    branch = f"choir/{issue}-{task_slug(record)}"

    # Assemble the working tree as a worktree of the shared per-project store
    # (one object DB, not N full clones; note 09 §1). Fall back to a full clone
    # if the store path fails for any reason — the task must still proceed.
    try:
        repo_store.create_worktree(
            repo, workdir=path, branch=branch, commit=record.project_ref.commit
        )
    except repo_store.RepoStoreError as e:
        print(f"repo-store unavailable ({e}); falling back to a full clone")
        _legacy_clone_checkout(repo, issue, record, path)

    (path / "TASK.md").write_text(body_prose.strip() + "\n", encoding="utf-8")
    # Resolve the prover profile from the pinned commit now on disk (the
    # worktree/clone just landed above) so the primer's facts (build
    # command, toolchain, protected files, trust patterns) match the
    # project's actual prover instead of assuming lean4.
    profile = workspace_profile(path)
    write_choir_md(path, profile)

    # Sync the LATEST skills (default-branch tip the clone just fetched)
    # into an out-of-tree, git-excluded location and assemble the agent's
    # context. Skills are guidance, not a build input — they track latest
    # while code/toolchain stay pinned to project_ref.commit (note 06).
    add_local_excludes(path, WORKSPACE_EXCLUDES)
    skills_commit = sync_skills(path) or ""
    assemble_skill_context(path)

    meta = LeaseMetadata(
        repo=repo,
        issue=issue,
        branch=branch,
        pinned_commit=record.project_ref.commit,
        claimed_at=datetime.now(UTC).isoformat(timespec="seconds"),
        claimed_by=claimed_by,
        target_file=record.target_file or "",
        target_decl=record.target_decl or "",
        task_type=record.type.value,
        skills_commit=skills_commit,
        session=session,
    )
    meta.write(path / ".choir-lease.json")

    return path


def _legacy_clone_checkout(
    repo: str, issue: int, record: TaskRecord, path: Path
) -> None:
    """Pre-note-09 behavior: a full `gh repo clone` + topic-branch checkout.

    The fallback when the shared repo-store / worktree path is unavailable
    (`RepoStoreError`). Produces the same working tree, just without sharing the
    object database.
    """
    _run(["gh", "repo", "clone", repo, str(path)])
    branch = f"choir/{issue}-{task_slug(record)}"
    try:
        _run(["git", "checkout", "-b", branch, record.project_ref.commit], cwd=path)
    except WorkspaceError:
        # If the pinned commit isn't reachable, fetch all refs and retry.
        _run(["git", "fetch", "--all"], cwd=path)
        _run(["git", "checkout", "-b", branch, record.project_ref.commit], cwd=path)


def _join_english(items: tuple[str, ...], *, conjunction: str) -> str:
    """Backtick + join `items`, Oxford-comma style, with `conjunction` last.

    `("sorry",)` -> "`sorry`"; `("a", "b")` -> "`a` <conj> `b`";
    `("a", "b", "c")` -> "`a`, `b`, <conj> `c`".
    """
    quoted = [f"`{i}`" for i in items]
    if len(quoted) == 1:
        return quoted[0]
    if len(quoted) == 2:
        return f"{quoted[0]} {conjunction} {quoted[1]}"
    return ", ".join(quoted[:-1]) + f", {conjunction} {quoted[-1]}"


def build_choir_md(profile: ProverProfile) -> str:
    """Render the agent-facing workspace primer for `profile`.

    Shared prose skeleton (design note 12 §6) — the facts that used to
    be hard-coded to Lean 4 (build command, toolchain pinning, protected
    files, placeholder tokens, trust-eroding patterns) are injected from
    `profile` instead, so the same primer serves any prover Choir ships
    a profile for.
    """
    build_cmd = " ".join(profile.build_command)

    if profile.toolchain_file:
        toolchain_line = (
            f"The toolchain is pinned in `{profile.toolchain_file}` in this "
            f"checkout. `{build_cmd}` works out of the box."
        )
    else:
        toolchain_line = (
            "The toolchain version is pinned via the task's "
            f"`project_ref.toolchain`, not a file in this checkout. "
            f"`{build_cmd}` works out of the box once that toolchain is installed."
        )

    protected = _join_english(
        (*profile.protected_files, ".choir/", ".github/", "skills/"),
        conjunction="and",
    )
    placeholders = _join_english(profile.placeholder_tokens, conjunction="or")
    trust_labels = _join_english(
        tuple(label for label, _pattern in profile.trust_patterns), conjunction="or"
    )

    return f"""\
# CHOIR.md — workspace primer for your agent

You are reading this from inside a Choir-managed workspace at
`~/.choir/work/<owner>/<repo>/<issue>/`. A human contributor claimed an
open issue on a project using the `{profile.name}` prover, and the
`choir` CLI prepared this directory for you. This file describes the
contract — read it once, then act.

## Your job

Produce one or more commits on the currently-checked-out topic branch
(`choir/<issue>-<slug>`) that close the task described in `TASK.md`.
That's it. When you exit cleanly, `choir submit` will push the branch
and open a PR linked to the issue.

You may push and open a PR yourself if you prefer — Choir's submit step
is idempotent and reuses an existing PR rather than creating a duplicate.

## Files in this directory

- `TASK.md` — the issue body verbatim. This is the spec.
- `.choir-lease.json` — metadata: `target_decl`, `target_file`,
  `task_type`, the GitHub repo and issue number. Read for context;
  don't edit.
- `.choir-context.md` — the maintainer's **current** project guidance
  (conventions, naming, forbidden patterns, dependency rules), synced to
  the latest at task start and assembled for you. **Read it and follow
  it** before making structural or stylistic decisions. (Absent only if
  the project has authored no skill pack yet — then use your defaults.)
  If a committed `skills/` directory exists at the pinned commit, treat it
  as a possibly-stale snapshot — `.choir-context.md` is the authoritative,
  current guidance.
  The raw synced files sit under `.choir-skills/`; both are Choir-managed
  and git-excluded — never edit or commit them.
- `CHOIR.md` — this file.
- `roadmap/`, when the project keeps one — the plan: `README.md` for the
  route, a file per group for the mathematics, and `graph.json` listing
  every declaration the plan knows about with what depends on what.
  Search it for lemmas the project already has before writing your own.
  Your task's `blueprint_ref`, when it has one, names your node.
- The rest of the directory is the project itself, checked out at the
  pinned commit recorded in `.choir-lease.json`.

## Pre-installed on the contributor's machine

- {toolchain_line}
- If you have library/lemma/theorem search tooling configured,
  use it before guessing or inventing a name — that's the single
  highest-leverage habit for proving here, whichever proof assistant
  this project uses.

## Conventions to follow

- **Stay focused on `target_decl`.** If you find adjacent issues you'd
  want to fix, leave them — the verify pipeline marks out-of-scope
  diffs for human attention.
- **Do not edit protected files**: {protected}. These are pinned by the
  project maintainer.
- **Do not introduce new {placeholders} placeholders.** Leaving one in
  place of a real proof is a documented return state, not a silent
  pass — the verify pipeline tracks net-new placeholders in your
  submission.
- **Do not introduce new {trust_labels}.** The verify pipeline flags
  these as trust-eroding and a human reviewer must approve before merge.
- **Do not weaken the statement of `target_decl` to make it provable.**
  The verify pipeline catches this: PRs whose base and head signatures
  differ for `target_decl` fail the statement-equivalence audit.
- **Write comments in final form.** Comment only where the code needs it,
  and only about what the code does now. Attempts, rejected approaches,
  and what changed since the last push never appear in the source.

## What you should NOT do

- Do not read or write files outside this workspace for credentials,
  API keys, or contributor state. Choir never proxies credentials and
  there are none to find here.
- Do not open new issues or PRs unrelated to `target_decl`.
- Do not force-push. The submit flow assumes a normal fast-forward.
"""


def workspace_profile(path: Path) -> ProverProfile:
    """Resolve the prover profile selected by `path`'s `.choir/project.toml`.

    Falls back to the lean4 profile (with a printed warning) if the read
    raises `ProverError` (malformed TOML, unknown prover name) or hits an
    `OSError` — a worker must not die on a project with a broken
    `project.toml`. Shared by `setup_workspace`, `task.py` and `cli.py` so
    the resolve-and-fallback logic lives in one place.
    """
    try:
        return get_profile(read_prover(path))
    except (ProverError, OSError) as e:
        print(f"warning: could not resolve prover for {path} ({e}); falling back to lean4")
        return LEAN4


def write_choir_md(workspace_path: Path, profile: ProverProfile) -> None:
    """Drop the agent-facing primer at `<workspace>/CHOIR.md`.

    Rendered from `profile` via `build_choir_md`; one drop per workspace.
    Refresh by re-running this function — it overwrites unconditionally,
    so contributors who clobbered the file get a clean copy on next
    setup.
    """
    (workspace_path / "CHOIR.md").write_text(build_choir_md(profile), encoding="utf-8")


def _info_exclude_path(workspace: Path) -> Path:
    """The repo's local-only `info/exclude` for `workspace`.

    Resolves the git *common* dir so this is correct in a worktree (where
    ``<ws>/.git`` is a file pointing at ``<store>/.git/worktrees/<id>`` and the
    shared exclude lives at ``<store>/.git/info/exclude``). Falls back to
    ``<ws>/.git/info/exclude`` when `workspace` isn't a real git repo (e.g. unit
    tests that stub the `.git/info` layout).
    """
    try:
        common = _capture(
            ["git", "-C", str(workspace), "rev-parse",
             "--path-format=absolute", "--git-common-dir"],
        )
        if common:
            return Path(common) / "info" / "exclude"
    except WorkspaceError:
        pass
    return workspace / ".git" / "info" / "exclude"


def add_local_excludes(workspace: Path, names: list[str]) -> None:
    """Append paths to the repo's local-only `info/exclude` (never committed).

    Idempotent. Worktree-safe via :func:`_info_exclude_path`; no-op if the
    resolved `info/` dir is absent.
    """
    exclude = _info_exclude_path(workspace)
    if not exclude.parent.is_dir():
        return
    existing = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
    present = set(existing.splitlines())
    new = [n for n in names if n not in present]
    if not new:
        return
    with exclude.open("a", encoding="utf-8") as fh:
        if existing and not existing.endswith("\n"):
            fh.write("\n")
        fh.write("# Choir: synced skills — never commit\n")
        for n in new:
            fh.write(n + "\n")


def _capture(cmd: list[str], *, cwd: Path | None = None) -> str:
    """Run a subprocess and return stdout; raise WorkspaceError on failure."""
    try:
        result = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, check=True
        )
    except FileNotFoundError as e:
        raise WorkspaceError(f"required command not found: {cmd[0]}") from e
    except subprocess.CalledProcessError as e:
        raise WorkspaceError(
            f"command failed: {' '.join(cmd)}\n{(e.stderr or '').strip()}"
        ) from e
    return result.stdout.strip()


def sync_skills(workspace: Path) -> str | None:
    """Extract the LATEST skills/ (default-branch tip the clone already
    fetched) into <workspace>/.choir-skills. Returns the synced commit
    SHA, or None if origin/HEAD can't be resolved. A repo with no skills/
    at the tip yields an empty .choir-skills and still returns the SHA.
    """
    try:
        sha = _capture(["git", "rev-parse", "origin/HEAD"], cwd=workspace)
    except WorkspaceError:
        return None
    skills_dir = workspace / ".choir-skills"
    skills_dir.mkdir(exist_ok=True)
    archive = subprocess.run(
        ["git", "archive", f"{sha}:skills"],
        cwd=workspace, capture_output=True, check=False,
    )
    if archive.returncode != 0:
        return sha  # no skills/ at the tip — project has no pack (yet)
    try:
        subprocess.run(
            ["tar", "-x", "-C", str(skills_dir)],
            input=archive.stdout, check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        raise WorkspaceError(f"failed to extract skills archive: {e}") from e
    return sha


_SKILL_CONTEXT_HEADER = (
    "# Project guidance (synced)\n\n"
    "The maintainer's CURRENT conventions and rules for this project — "
    "synced to the latest at the start of this task. Follow them. Your "
    "specific task is in TASK.md.\n\n"
)


def assemble_skill_context(workspace: Path) -> Path | None:
    """Concatenate synced skill files into <workspace>/.choir-context.md.
    Returns the path, or None if there are no skill files to assemble.
    """
    skills_dir = workspace / ".choir-skills"
    if not skills_dir.is_dir():
        return None
    parts: list[str] = []
    for md in sorted(skills_dir.rglob("*.md")):
        rel = md.relative_to(skills_dir)
        parts.append(f"## {rel}\n\n{md.read_text(encoding='utf-8').strip()}\n")
    if not parts:
        return None
    out = workspace / ".choir-context.md"
    out.write_text(_SKILL_CONTEXT_HEADER + "\n".join(parts), encoding="utf-8")
    return out


def _run(cmd: list[str], *, cwd: Path | None = None) -> None:
    """Run a subprocess; raise WorkspaceError with stderr context on failure."""
    try:
        subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, check=True
        )
    except FileNotFoundError as e:
        raise WorkspaceError(
            f"required command not found: {cmd[0]}; "
            "is it installed and on PATH?"
        ) from e
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or "").strip()
        raise WorkspaceError(
            f"command failed: {' '.join(cmd)}\n{stderr}"
        ) from e
