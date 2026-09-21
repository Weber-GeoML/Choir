"""One call: from a project checkout to a written page.

The rest of `viz/` takes explicit inputs — which repo, which checkout,
which payload. That is right for a pipeline and wrong for the thing an
overseer actually wants, which is to stand in a project folder and ask for
the picture. Everything the pipeline needs is already in the folder: git
knows the checkout root and the remote knows the slug, so nothing has to
be typed.

Reads only. Nothing here writes to the project repo or to GitHub; the one
file it writes is the page.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from gate.provers import PROFILES, ProverProfile
from gate.provers.select import read_prover
from viz.derive import derive
from viz.github import Payload, fetch
from viz.gitscan import scan_history
from viz.render import render, summary


class ReportError(RuntimeError):
    """The folder is not a project this tool can read."""


_SLUG = re.compile(
    r"""(?:git@|https://|ssh://git@)
        (?:[^/:]+)[/:]
        (?P<owner>[^/]+)/(?P<name>[^/]+?)(?:\.git)?$""",
    re.X,
)

# `origin` in a contributor's clone is their fork, and the timeline is the
# upstream project's, so a named upstream wins. `--repo` overrides both.
_REMOTES = ("upstream", "origin")


@dataclass(frozen=True)
class Discovered:
    repo: str
    checkout: Path
    prover: str


def _git(*args: str, cwd: Path) -> str:
    try:
        done = subprocess.run(
            ("git", *args), cwd=cwd, capture_output=True, text=True, check=True
        )
    except (OSError, subprocess.CalledProcessError) as e:
        raise ReportError(f"git {' '.join(args)} failed: {e}") from e
    return done.stdout.strip()


def discover(start: Path | None = None, *, repo: str | None = None) -> Discovered:
    """What the folder already knows: its root, its slug, its prover."""
    here = Path(start or Path.cwd()).expanduser().resolve()
    root = Path(_git("rev-parse", "--show-toplevel", cwd=here))
    if repo is None:
        for name in _REMOTES:
            url = _git("remote", "get-url", name, cwd=root) if _has(root, name) else ""
            found = _SLUG.match(url) if url else None
            if found:
                repo = f"{found['owner']}/{found['name']}"
                break
    if repo is None:
        raise ReportError(
            f"{root} has no GitHub remote this tool recognises; pass the "
            "owner/name explicitly"
        )
    try:
        prover = read_prover(root)
    except Exception:
        # A picture of the wrong prover's declarations is recoverable; a
        # tool that refuses to run is not.
        prover = "lean4"
    return Discovered(repo=repo, checkout=root, prover=prover)


def _has(root: Path, remote: str) -> bool:
    return remote in _git("remote", cwd=root).split()


def _graph(checkout: Path) -> dict | None:
    path = checkout / "roadmap" / "graph.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def profile_of(prover: str) -> ProverProfile:
    return PROFILES.get(prover, PROFILES["lean4"])


def build_report(
    *,
    repo: str,
    checkout: Path,
    prover: str,
    out: Path,
    payload: Payload | None = None,
    branch: str = "HEAD",
    scope: str = "plan",
) -> dict[str, object]:
    """Fetch, scan, derive, render, write. Returns what was drawn."""
    if payload is None:
        payload = fetch(repo)
    commits = scan_history(checkout, branch=branch, profile=profile_of(prover))
    timeline = derive(payload, commits, prover=prover, graph=_graph(checkout))
    out.write_text(render(timeline, scope=scope), encoding="utf-8")
    drawn = summary(timeline, scope=scope)
    return {
        "out": str(out),
        "repo": repo,
        "checkout": str(checkout),
        "prover": prover,
        "scope": scope,
        **drawn,
        "declarations_in_history": len(timeline.nodes),
        "notes": list(timeline.notes),
    }


DEFAULT_NAME = "choir-proof-tree.html"


def report(
    start: Path | None = None,
    *,
    repo: str | None = None,
    out: Path | None = None,
    scope: str = "plan",
    branch: str = "HEAD",
) -> dict[str, object]:
    """The whole thing, from wherever you are standing."""
    found = discover(start, repo=repo)
    target = Path(out) if out else Path(start or Path.cwd()) / DEFAULT_NAME
    return build_report(
        repo=found.repo,
        checkout=found.checkout,
        prover=found.prover,
        out=target.expanduser(),
        branch=branch,
        scope=scope,
    )


__all__ = ["DEFAULT_NAME", "Discovered", "ReportError", "discover", "report"]
