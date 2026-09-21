"""The overlay install manifest: what this tool installed into a project.

`upgrade-project.sh` installs some surfaces with `rsync -a --delete`
(`gate/`, `orchestrator/`, `client/`, `.github/ISSUE_TEMPLATE/`), which
self-retire: a file Choir stops shipping disappears from the project on the
next upgrade. The surfaces installed file-by-file — `.github/workflows/*`,
the root files, `.choir/*` — had no such property, so a workflow Choir
retired stayed behind forever, invoking code the same upgrade had deleted.
That is not hypothetical: spec D1 retired the review layer and deleted
`gate/verify/review_gate_cli.py`, while every upgraded project kept
`.github/workflows/verify-review.yml` calling it — red on every PR.

This module is the record that gives those surfaces the same property.
Each run writes the set of paths it considers part of the overlay; the next
run retires `previous - current`. Retirement is therefore *derived* from the
difference between two install sets, not declared: dropping a template from
a script's copy list is the entire act of retiring it, so there is no second
place to update and none to forget.

Two properties matter more than the mechanism:

- **It can only ever remove what its own lineage installed.** The manifest
  describes what *this* Choir put into *this* repo, so a file no manifest
  claims — an overseer's own workflow, a fork's added check, an unrelated CI
  file — is invisible here. There is no global list of Choir's history to
  misapply, which is what makes this safe once people run modified Choirs.
- **Absent and corrupt are different.** An absent manifest is every
  pre-manifest repo and means "seed one". A corrupt manifest raises: reading
  it as absent would silently restart tracking from scratch and quietly stop
  retiring anything — the same absence-vs-failure fail-open
  `gate/verify/base_contents.py` had to close.

Serialization carries no timestamp and no provenance on purpose.
`upgrade-project.sh` decides "already up to date" by staging the overlay
paths and checking `git diff --cached` is empty; a field that changed every
run would make that path unreachable and turn every re-run into an empty
commit. Provenance already lives in `.choir/project.toml`'s `choir_commit`
(`gate/upgrade/pin.py`).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath

MANIFEST_VERSION = 1
"""Schema version of this file, deliberately independent of
`PROTOCOL_VERSION`: nothing outside the overseer's own tooling reads the
manifest, so it can evolve without a wire-protocol bump."""

MANIFEST_RELPATH = ".choir/overlay.json"

CLASS_MANAGED = "managed"
"""Choir rewrites the file on every upgrade, so retirement deletes it."""

CLASS_SEEDED = "seeded"
"""Choir writes the file once at bootstrap and the overseer owns it after,
so retirement only ever reports it."""

_CLASSES = frozenset({CLASS_MANAGED, CLASS_SEEDED})


class ManifestError(RuntimeError):
    """The manifest is unusable, or a path in it is unsafe to act on."""


@dataclass(frozen=True)
class Entry:
    path: str
    cls: str
    sha256: str


@dataclass(frozen=True)
class Manifest:
    manifest_version: int
    files: dict[str, Entry]


def validate_relpath(path: str) -> str:
    """Return `path` unchanged, or raise if it is unsafe to delete.

    The deleting caller resolves these against a repo root, so anything
    absolute or containing a `..` component could reach outside the project.
    A hand-edited or corrupted manifest must not be able to do that, which
    is why validation lives at parse time rather than at delete time.
    """
    if not path or path in {".", "./"}:
        raise ManifestError("manifest path is empty")
    pure = PurePosixPath(path)
    if pure.is_absolute():
        raise ManifestError(f"manifest path is absolute: {path!r}")
    if ".." in pure.parts:
        raise ManifestError(f"manifest path escapes the repo: {path!r}")
    return path


def build_manifest(entries: Iterable[Entry]) -> Manifest:
    files: dict[str, Entry] = {}
    for entry in entries:
        validate_relpath(entry.path)
        if entry.cls not in _CLASSES:
            raise ManifestError(f"unknown ownership class: {entry.cls!r}")
        files[entry.path] = entry
    return Manifest(manifest_version=MANIFEST_VERSION, files=files)


def load_manifest(text: str) -> Manifest:
    try:
        raw = json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ManifestError(f"manifest is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ManifestError("manifest is not a JSON object")
    version = raw.get("manifest_version")
    if version != MANIFEST_VERSION:
        raise ManifestError(
            f"unsupported manifest_version {version!r} (expected {MANIFEST_VERSION})"
        )
    files_raw = raw.get("files")
    if not isinstance(files_raw, dict):
        raise ManifestError("manifest 'files' is not a JSON object")
    entries: list[Entry] = []
    for path, meta in files_raw.items():
        if not isinstance(meta, dict):
            raise ManifestError(f"manifest entry for {path!r} is not an object")
        cls = meta.get("class")
        sha = meta.get("sha256")
        if not isinstance(cls, str) or cls not in _CLASSES:
            raise ManifestError(f"manifest entry for {path!r} has bad class {cls!r}")
        if not isinstance(sha, str) or not sha:
            raise ManifestError(f"manifest entry for {path!r} has bad sha256")
        entries.append(Entry(validate_relpath(path), cls, sha))
    return build_manifest(entries)


def serialize(manifest: Manifest) -> str:
    payload = {
        "manifest_version": manifest.manifest_version,
        "files": {
            entry.path: {"class": entry.cls, "sha256": entry.sha256}
            for entry in manifest.files.values()
        },
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


class Action(Enum):
    """What to do with a path the current overlay set no longer contains."""

    DELETE = "delete"
    REPORT = "report"
    ALREADY_GONE = "already-gone"


@dataclass(frozen=True)
class Retirement:
    path: str
    action: Action
    reason: str


def plan_retirements(
    previous: Manifest,
    overlay: set[str],
    on_disk: Mapping[str, str | None],
) -> list[Retirement]:
    """What to do with each path the previous run installed and this one does not.

    `overlay` is the set of paths Choir considers part of the overlay for
    this project and prover — *not* the paths this run happened to write.
    The distinction is load-bearing: `verify-pr.yml` and `.choir/verify.toml`
    are written at bootstrap and deliberately left alone by every later
    upgrade, so an overlay set defined as "what this run wrote" would find
    them missing on every subsequent run and report them as retired forever.

    `on_disk` maps each candidate path to its current sha256, or `None` when
    the file is absent — injected so the whole decision table is testable as
    data.
    """
    plan: list[Retirement] = []
    for path in sorted(set(previous.files) - overlay):
        entry = previous.files[path]
        current = on_disk.get(path)
        if current is None:
            plan.append(
                Retirement(path, Action.ALREADY_GONE, "no longer shipped; already absent")
            )
        elif entry.cls == CLASS_SEEDED:
            plan.append(
                Retirement(path, Action.REPORT, "seeded: no longer shipped; yours to remove")
            )
        elif current == entry.sha256:
            plan.append(Retirement(path, Action.DELETE, "no longer shipped"))
        else:
            plan.append(
                Retirement(
                    path,
                    Action.REPORT,
                    "no longer shipped, and edited locally; left in place",
                )
            )
    return plan


_NOTICE_DIRS = (".github/workflows/", ".choir/")


def unmanaged_notices(tracked: Iterable[str], overlay: set[str]) -> list[str]:
    """Tracked files in Choir's two file-by-file directories that no manifest claims.

    Printed only in seed mode, where there is no previous manifest to diff
    and therefore no way to know what a pre-manifest Choir left behind.
    Report-only, and it claims nothing about ownership: a file here may be
    Choir's leftovers or entirely the overseer's. Restricting to *tracked*
    files is what keeps the overseer's gitignored `.choir/orchestrator.toml`
    out of it.
    """
    return sorted(
        path for path in tracked if path not in overlay and path.startswith(_NOTICE_DIRS)
    )


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tracked_files(root: Path) -> list[str]:
    """`git ls-files` for `root`, or `[]` if it is not a usable git repo.

    Best-effort on purpose: the seed-mode notice this feeds is advisory, and
    a bootstrap runs before the first commit exists.
    """
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Record the overlay install set for a project and retire paths a "
            "previous run installed that the current one no longer ships."
        )
    )
    parser.add_argument("root", type=Path, help="Path to the project repo root")
    parser.add_argument(
        "--managed",
        action="append",
        default=[],
        metavar="PATH",
        help="Repo-relative path Choir rewrites every upgrade (retirement deletes it)",
    )
    parser.add_argument(
        "--seeded",
        action="append",
        default=[],
        metavar="PATH",
        help="Repo-relative path the overseer owns after bootstrap (retirement reports)",
    )
    args = parser.parse_args(argv)

    root: Path = args.root
    try:
        declared = [Entry(validate_relpath(p), CLASS_MANAGED, "") for p in args.managed] + [
            Entry(validate_relpath(p), CLASS_SEEDED, "") for p in args.seeded
        ]
    except ManifestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    overlay = {entry.path for entry in declared}
    manifest_path = root / MANIFEST_RELPATH

    previous: Manifest | None = None
    if manifest_path.exists():
        try:
            previous = load_manifest(manifest_path.read_text(encoding="utf-8"))
        except (ManifestError, OSError, UnicodeDecodeError) as exc:
            print(f"error: {MANIFEST_RELPATH}: {exc}", file=sys.stderr)
            print(
                "refusing to continue: a manifest that cannot be read is not "
                "evidence that nothing was installed. Inspect it, or delete it "
                "to restart retirement tracking deliberately.",
                file=sys.stderr,
            )
            return 2

    if previous is not None:
        candidates = set(previous.files) - overlay
        on_disk = {
            path: (_sha256_file(root / path) if (root / path).is_file() else None)
            for path in candidates
        }
        for retirement in plan_retirements(previous, overlay, on_disk):
            if retirement.action is Action.DELETE:
                (root / retirement.path).unlink()
                print(f"deleted\t{retirement.path}")
            elif retirement.action is Action.REPORT:
                print(f"kept\t{retirement.path}\t{retirement.reason}")
    else:
        for path in unmanaged_notices(_tracked_files(root), overlay):
            print(f"unmanaged\t{path}")

    # A declared path that does not exist on disk is omitted rather than
    # recorded with an empty hash, so a prover-conditional file
    # (verify-trust-report.yml on isabelle/rocq) never enters the manifest
    # and therefore never looks retired later.
    hashed = [
        Entry(entry.path, entry.cls, _sha256_file(root / entry.path))
        for entry in declared
        if (root / entry.path).is_file()
    ]
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(serialize(build_manifest(hashed)), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
