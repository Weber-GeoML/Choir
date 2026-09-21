"""Machine-local shared Mathlib (dependency) store — one copy per dependency
set, shared by every workspace that pins it.

Every Choir workspace for a project pins the same dependency set (its committed
`lake-manifest.json`), so each one's `.lake/packages` — Mathlib + transitive
deps, several GB of oleans — is byte-identical. Fetching it per workspace
(`lake exe cache get`) costs N x several GB and fills disks fast.

Instead Choir keeps ONE copy per dependency set under
`~/.choir/mathlib-store/<key>/` and points each workspace's `.lake/packages` at
it with a symlink: no download, no copy, and the same bytes serve every
workspace. This is safe because Lake reads the dependency tree and does not
write to it — a build that recompiles project modules touches zero files under
`.lake/packages`. The project's OWN `.lake/build` IS written on every build,
which is why `client.build_store` clones that instead of linking it.

The store is keyed by the *resolved dependency set* (the manifest's package
revs, not its raw text — see `manifest_key`), so two different projects pinning
the same Mathlib share one entry, while a toolchain/Mathlib bump changes a rev
and transparently gets a fresh store.

A symlink works on every filesystem, so there is no copy-on-write requirement
and no per-filesystem fallback. The store is delete-able to reclaim space, but
NOT while a task is in flight: a live workspace links into it, and removing it
leaves that workspace without dependencies until the next prepare clears the
stale link and repopulates.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

MANIFEST_NAME = "lake-manifest.json"
PACKAGES_REL = Path(".lake") / "packages"


def store_root() -> Path:
    """Root of the shared dependency store. `$CHOIR_MATHLIB_STORE` overrides."""
    override = os.environ.get("CHOIR_MATHLIB_STORE")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".choir" / "mathlib-store"


def _dep_signature(manifest_text: str) -> str | None:
    """Canonical JSON of the *resolved dependency set* — for each package its
    `name`, `url`, `rev`, `subDir` — sorted and order-independent. Returns None
    if the manifest isn't the expected JSON shape, so the caller falls back to
    raw-text hashing.

    Deliberately excludes everything project-specific: the manifest's root
    package `name`/`lakeDir`/`packagesDir`/`version` and each dep's `inputRev`
    (how the pin was spelled) and `inherited`/`scope`. None of those change the
    bytes in `.lake/packages`; the resolved `rev` (a git SHA) does. So two
    different projects pinning the identical deps produce the same signature.
    """
    try:
        data = json.loads(manifest_text)
        packages = data["packages"]
    except (json.JSONDecodeError, TypeError, KeyError):
        return None
    if not isinstance(packages, list):
        return None
    deps: list[dict[str, object]] = []
    for pkg in packages:
        if not isinstance(pkg, dict):
            return None
        deps.append({k: pkg.get(k) for k in ("name", "url", "rev", "subDir")})
    deps.sort(key=lambda d: tuple(str(d[k] or "") for k in ("name", "url", "rev", "subDir")))
    return json.dumps(deps, sort_keys=True)


def manifest_key(manifest_text: str) -> str:
    """Stable key for a dependency set = hash of its resolved deps.

    Keys on the dependency set (`_dep_signature`), not the raw manifest text,
    so two DIFFERENT projects that pin the identical Mathlib + transitive deps
    share ONE store entry instead of each keeping a multi-GB copy. A
    toolchain/Mathlib bump changes a `rev`, which changes the key → fresh store
    entry. Non-standard manifests fall back to raw-text hashing (distinct raw
    inputs stay distinct)."""
    signature = _dep_signature(manifest_text)
    payload = signature if signature is not None else manifest_text
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _packages_present(pkgs: Path) -> bool:
    return pkgs.is_dir() and any(pkgs.iterdir())


def link_store(store_pkgs: Path, pkgs: Path) -> bool:
    """Point the workspace's `pkgs` at the store's `store_pkgs`. True on success.

    The link is always the whole `.lake/packages` directory. Linking an
    individual package inside an otherwise-real packages directory invalidates
    Lake's traces and forces a full dependency rebuild.
    """
    if pkgs.exists() or pkgs.is_symlink():
        return False
    try:
        pkgs.parent.mkdir(parents=True, exist_ok=True)
        pkgs.symlink_to(store_pkgs, target_is_directory=True)
    except OSError:
        return False
    return True


def _clear_empty_packages(pkgs: Path) -> None:
    """Remove a `.lake/packages` holding nothing, so prepare can link or fetch.

    Two ways to get one: a link whose store entry was reclaimed (the store is
    reclaimable space, so a workspace can outlive its entry), and an empty
    directory left by an interrupted run. Neither holds dependencies, and both
    would otherwise block a fresh link and force a needless refetch.
    """
    if _packages_present(pkgs):
        return
    if pkgs.is_symlink():
        pkgs.unlink(missing_ok=True)
    elif pkgs.is_dir():
        with contextlib.suppress(OSError):
            pkgs.rmdir()  # refuses a populated directory


def _default_cache_get(workspace: Path) -> bool:
    """Run `lake exe cache get` in `workspace` to materialise + fetch deps."""
    try:
        result = subprocess.run(
            ["lake", "exe", "cache", "get"],
            cwd=workspace,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        print("mathlib-cache: `lake` not found; skipping dep prefetch", file=sys.stderr)
        return False
    if result.returncode != 0:
        print(
            "mathlib-cache: `lake exe cache get` failed: "
            f"{(result.stderr or '').strip()[:200]}",
            file=sys.stderr,
        )
        return False
    return True


def prepare_workspace_deps(
    workspace: Path,
    *,
    cache_get: Callable[[Path], bool] | None = None,
) -> str:
    """Ensure `<workspace>/.lake/packages` is populated, sharing via the store.

    Idempotent and best-effort — never raises; on any failure it returns a
    status and lets the normal `lake exe cache get` proceed. Returns:

      ``"no-manifest"``      not a Lake/Mathlib project — nothing to do
      ``"present"``          packages already there (resume / re-run)
      ``"hit"``              linked to the shared store (no download, no copy)
      ``"fetched"``          miss: fetched, moved into the store, and linked
      ``"fetched-nostore"``  miss: fetched, but the store could not be written
      ``"skipped"``          fetch unavailable/failed — the build will handle it

    `cache_get(workspace) -> bool` runs the fetch (injected in tests).
    """
    manifest = workspace / MANIFEST_NAME
    if not manifest.is_file():
        return "no-manifest"

    pkgs = workspace / PACKAGES_REL
    _clear_empty_packages(pkgs)
    if _packages_present(pkgs):
        return "present"

    key = manifest_key(manifest.read_text(encoding="utf-8"))
    store_pkgs = store_root() / key / PACKAGES_REL

    # HIT: link to the deps already on this machine — no fetch, no copy.
    if _packages_present(store_pkgs) and link_store(store_pkgs, pkgs):
        print(f"mathlib-cache: linked deps from store [{key}] — no fetch needed")
        return "hit"

    # MISS: fetch, then move into the store so the next workspace is a HIT.
    fetch = cache_get if cache_get is not None else _default_cache_get
    if not fetch(workspace) or not _packages_present(pkgs):
        return "skipped"
    return _promote_to_store(pkgs, store_pkgs, key)


def _promote_to_store(pkgs: Path, store_pkgs: Path, key: str) -> str:
    """Move freshly fetched deps into the store and link the workspace at them.

    Moving rather than copying keeps one copy on disk; the workspace and the
    store live under `~/.choir`, so this is a rename. Every failure path leaves
    the workspace with usable dependencies.
    """
    if _packages_present(store_pkgs):
        # Another workspace seeded the entry while this one fetched. Keep one
        # copy rather than two, but not at the cost of this workspace's deps.
        superseded = pkgs.with_name(pkgs.name + ".superseded")
        try:
            os.replace(pkgs, superseded)
        except OSError:
            return "fetched-nostore"
        if link_store(store_pkgs, pkgs):
            shutil.rmtree(superseded, ignore_errors=True)
            return "fetched"
        os.replace(superseded, pkgs)
        return "fetched-nostore"

    try:
        store_pkgs.parent.mkdir(parents=True, exist_ok=True)
        os.replace(pkgs, store_pkgs)
    except OSError:
        return "fetched-nostore"
    if link_store(store_pkgs, pkgs):
        print(f"mathlib-cache: saved deps to store [{key}] for reuse")
        return "fetched"
    os.replace(store_pkgs, pkgs)
    return "fetched-nostore"
