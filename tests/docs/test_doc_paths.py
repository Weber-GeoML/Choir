"""Guard: every docs/ path referenced by shipping code resolves.

Agents follow these paths at runtime, so a dangling one is a broken
contract. AGENTS.md is excluded: development-facing, not shipped.

`docs` is scanned, since docs cross-reference each other. Dated record
inside it is not — a dev-log entry, a superseded plan or spec, a dated
report and an archived doc each state what was true on their date,
paths included, and must not be rewritten to track a later move.
Living documentation tracks moves like any other consumer.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

SHIPPING_DIRS = (
    "gate",
    "orchestrator",
    "client",
    "scripts",
    "plugin",
    "templates",
    "samples",
    ".github",
    "docs",
)
SHIPPING_FILES = ("README.md", "AGENTS.md")  # AGENTS.md is public-branch-only
SUFFIXES = {".py", ".sh", ".md", ".yml", ".yaml", ".json", ".toml"}
EXCLUDED_DOC_PARTS = {"dev-log", "superpowers", "plans", "reports", "archive"}

DOC_PATH = re.compile(r"docs/[A-Za-z0-9_./-]*\.md")


def _is_dated_historical_record(path: Path, root: Path) -> bool:
    """True if `path` sits under an excluded dated-record subdirectory of `root`.

    Only components *below* `root` count. `path` is normally absolute
    (from `Path.rglob`), and its ancestors above `root` — e.g. the repo
    checkout itself sitting under a directory literally named `plans`
    or `reports` (an ordinary worktree name) — must never trigger this,
    or the guard silently stops scanning all of `docs/` with no test
    failure to reveal it.
    """
    return bool(EXCLUDED_DOC_PARTS & set(path.relative_to(root).parts))


def _shipping_files() -> list[Path]:
    out: list[Path] = []
    for name in SHIPPING_DIRS:
        root = REPO_ROOT / name
        if not root.exists():
            continue
        for p in root.rglob("*"):
            if not p.is_file() or p.suffix not in SUFFIXES:
                continue
            if name == "docs" and _is_dated_historical_record(p, root):
                continue
            out.append(p)
    out.extend(
        path
        for path in (REPO_ROOT / name for name in SHIPPING_FILES)
        if path.is_file()
    )
    return out


def test_dated_historical_record_predicate_ignores_ancestor_dirs() -> None:
    """The exclusion must look only below the scan root, never above it.

    Regression guard for a real defect: the predicate used to test
    `set(path.parts)` against the *absolute* path, so any ancestor of
    the repo checkout happening to be named "plans", "reports", etc.
    (an ordinary worktree name — this project's own convention is
    `.claude/worktrees/<name>`) silently excluded the *entire* `docs/`
    tree from the scan, with no test failure to reveal it. Constructed
    directly (no filesystem access needed) so it exercises the
    predicate in isolation.
    """
    root = Path("/repo/docs")

    # A normal, non-excluded file directly under the scanned root.
    assert not _is_dated_historical_record(root / "design" / "01.md", root)

    # A file under one of the actually-excluded subdirectories.
    assert _is_dated_historical_record(root / "dev-log" / "x.md", root)
    assert _is_dated_historical_record(root / "plans" / "x.md", root)

    # The defect: an ancestor of the scan root sharing a name with an
    # excluded subdirectory must NOT cause exclusion. Every excluded
    # name is exercised as an ancestor component here.
    for bad_ancestor in ("plans", "reports", "archive", "dev-log", "superpowers"):
        tricky_root = Path(f"/{bad_ancestor}/checkout/docs")
        target = tricky_root / "design" / "01.md"
        assert not _is_dated_historical_record(target, tricky_root), (
            f"ancestor directory named {bad_ancestor!r} incorrectly "
            "excluded a file that is not under any excluded subdirectory"
        )


def test_every_referenced_doc_path_exists() -> None:
    missing: list[str] = []
    for path in _shipping_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for ref in DOC_PATH.findall(text):
            if not (REPO_ROOT / ref).exists():
                missing.append(f"{path.relative_to(REPO_ROOT)} -> {ref}")
    assert not missing, "shipping files reference docs that do not exist:\n" + "\n".join(
        sorted(set(missing))
    )
