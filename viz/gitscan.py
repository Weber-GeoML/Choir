"""Per-commit declaration state, read from a project's git history.

This is what makes the timeline possible without a prover. Walking the
default branch in order and re-scanning only the files each commit
touched gives, for every declaration, the commit where it first appeared
and the commit where its placeholder body went away. Both are the
questions a timeline needs, and both are answerable from source text.

Cost is bounded by *changed* files rather than commits × files, so a
project with a few hundred commits scans in seconds.

The scan is textual, so it inherits the inventory scanner's limits: a
declaration header written inside a string literal reads as a
declaration. That misattributes a node in a picture; it decides nothing.
An anonymously written declaration is dropped rather than drawn under
whatever token follows the keyword — see `_named`.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from gate.inventory.scan import declaration_names, scan_text
from gate.provers.base import ProverProfile
from viz.model import utc


class GitScanError(RuntimeError):
    """A git command failed — a bad path, ref, or not a repository."""


@dataclass(frozen=True)
class DeclState:
    """One declaration as of some commit."""

    decl: str
    file: str
    has_placeholder: bool


@dataclass(frozen=True)
class CommitRecord:
    """What one commit did to the set of declarations.

    `stated` are declarations this commit introduced, `filled` are those
    whose placeholder body went away in it, and `removed` are those it
    deleted. A declaration introduced with no placeholder appears in `stated`
    with `has_placeholder` false and never appears in `filled`.
    """

    sha: str
    at: str
    """When, normalised to UTC — see `viz.model.utc`.

    Git reports the committer's local offset while GitHub reports UTC,
    and the timeline sorts on the string, so the two sources have to
    agree on a timezone before they can be interleaved."""
    subject: str
    author: str
    index: int = 0
    """Position in the branch's history, oldest first.

    Commit timestamps are second-granularity and a bulk push can land
    many commits in one second, so the log's own order is the only total
    order available. Timeline steps follow it.
    """
    stated: tuple[DeclState, ...] = ()
    filled: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "at", utc(self.at))


def _run(*args: str, cwd: Path) -> str:
    try:
        out = subprocess.run(
            args, cwd=cwd, capture_output=True, text=True, check=True
        )
    except (subprocess.CalledProcessError, OSError) as e:
        raise GitScanError(f"{' '.join(args)}: {e}") from e
    return out.stdout


def _named(name: str) -> bool:
    """Whether a scanned token is a name at all.

    A declaration may be written anonymously — lean4's `instance :
    IsProbabilityMeasure fairCoin := …` names nothing, and the prover
    mints the name itself. A textual scanner has no name to read there
    and takes the next token, which is punctuation or the head of a
    binder. Such a declaration is real, and the plan records it under
    the name the prover gave it; what a picture must not do is invent a
    node called `(f` or `:` beside it.
    """
    return bool(name) and (name[0].isalpha() or name[0] == "_")


def _file_decls(
    text: str, path: str, *, profile: ProverProfile
) -> dict[str, DeclState]:
    """Every declaration in one file, and whether it carries a placeholder.

    Both halves come from the inventory scanner, so a name here is the
    same string the placeholder audit attributes a placeholder to.
    """
    names = declaration_names(text, profile=profile)
    _axioms, sorries = scan_text(text, path, profile=profile)
    sorried = {s.decl for s in sorries if s.decl}
    return {
        name: DeclState(decl=name, file=path, has_placeholder=name in sorried)
        for name in names
        if _named(name)
    }


def _changed_files(sha: str, *, cwd: Path, profile: ProverProfile) -> list[str]:
    """Prover source files this commit touched, added or deleted."""
    raw = _run(
        "git",
        "show",
        "--pretty=format:",
        "--name-only",
        "--no-renames",
        sha,
        cwd=cwd,
    )
    exts = tuple(profile.file_extensions)
    return [
        line.strip()
        for line in raw.splitlines()
        if line.strip() and line.strip().endswith(exts)
    ]


def _blob(sha: str, path: str, *, cwd: Path) -> str:
    """File contents at a commit, or '' when absent (deleted or new)."""
    try:
        return _run("git", "show", f"{sha}:{path}", cwd=cwd)
    except GitScanError:
        return ""


def scan_history(
    repo_path: Path, *, branch: str = "HEAD", profile: ProverProfile
) -> list[CommitRecord]:
    """Walk `branch` oldest-first and report each commit's declaration changes.

    Only the files a commit touched are re-read, with running state
    carried between commits, so the work is proportional to the diff
    rather than to history × tree.
    """
    log = _run(
        "git",
        "log",
        "--reverse",
        "--format=%H%x1f%cI%x1f%s%x1f%an",
        branch,
        cwd=repo_path,
    )
    records: list[CommitRecord] = []
    index = 0
    # decl -> its state as of the previous commit
    live: dict[str, DeclState] = {}

    for line in log.splitlines():
        if not line.strip():
            continue
        sha, at, subject, author = [*line.split("\x1f"), "", "", ""][:4]
        index += 1
        touched = _changed_files(sha, cwd=repo_path, profile=profile)
        if not touched:
            continue

        stated: list[DeclState] = []
        filled: list[str] = []
        removed: list[str] = []

        for path in touched:
            before = {d: s for d, s in live.items() if s.file == path}
            after = _file_decls(_blob(sha, path, cwd=repo_path), path, profile=profile)

            for decl, state in after.items():
                prior = before.get(decl)
                if prior is None:
                    stated.append(state)
                elif prior.has_placeholder and not state.has_placeholder:
                    filled.append(decl)
                live[decl] = state

            for decl in before:
                if decl not in after:
                    removed.append(decl)
                    live.pop(decl, None)

        records.append(
            CommitRecord(
                sha=sha,
                at=at,
                subject=subject,
                author=author,
                index=index,
                stated=tuple(stated),
                filled=tuple(filled),
                removed=tuple(removed),
            )
        )
    return records
