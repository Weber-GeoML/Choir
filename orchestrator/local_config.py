"""Reader for `~/.choir/orchestrator.toml` — the overseer's LOCAL config.

This file lives on the overseer's machine, in the home directory,
**outside any repository**. It is never committed, never pushed, and
its contents are never copied into the project repo or its roadmap. The
orchestrator fills it in by interviewing the overseer on first run
(see `docs/agents/orchestrator-setup.md`, "Your local config").

Contrast with the two repo-side files:
- `.choir/project.toml` (automation level) — shared project policy,
  committed, applies to whoever runs the orchestrator.
- `.choir/verify.toml` (audit policy) — shared, committed, enforced
  by the gate.
This file holds what is private to one overseer's machine: paths,
cadence, session preferences.

Format::

    # ~/.choir/orchestrator.toml — local; never commit.
    [project]
    repo = "owner/name"
    checkout = "~/projects/name"     # local clone the orchestrator works in

    [loop]
    poll_interval_seconds = 600      # how often the poller re-checks GitHub
    max_wait_seconds = 3600          # poller gives up waiting and exits anyway

All fields optional; defaults below. Unknown sections/keys are ignored
(forward-compat).
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

# Legacy single-project location (pre-multi-project). Still read as a fallback
# so existing setups keep working; new configs go under PROJECTS_DIR.
ORCHESTRATOR_CONFIG_PATH = Path.home() / ".choir" / "orchestrator.toml"
PROJECTS_DIR = Path.home() / ".choir" / "projects"

DEFAULT_POLL_INTERVAL_SECONDS = 600
DEFAULT_MAX_WAIT_SECONDS = 3600


class LocalConfigError(RuntimeError):
    """Loading or validating `~/.choir/orchestrator.toml` failed."""


@dataclass(frozen=True)
class LocalConfig:
    repo: str | None = None
    checkout: Path | None = None
    poll_interval_seconds: int = DEFAULT_POLL_INTERVAL_SECONDS
    max_wait_seconds: int = DEFAULT_MAX_WAIT_SECONDS


def _require_positive_int(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise LocalConfigError(f"{field} must be a positive integer, got {value!r}")
    return value


def parse_local_config(text: str) -> LocalConfig:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise LocalConfigError(f"malformed TOML — {e}") from e

    project = data.get("project", {}) or {}
    loop = data.get("loop", {}) or {}
    if not isinstance(project, dict) or not isinstance(loop, dict):
        raise LocalConfigError("'project' and 'loop' must be tables")

    repo = project.get("repo")
    if repo is not None and not isinstance(repo, str):
        raise LocalConfigError("project.repo must be a string")

    checkout_raw = project.get("checkout")
    checkout: Path | None = None
    if checkout_raw is not None:
        if not isinstance(checkout_raw, str):
            raise LocalConfigError("project.checkout must be a string path")
        checkout = Path(checkout_raw).expanduser()

    interval = loop.get("poll_interval_seconds", DEFAULT_POLL_INTERVAL_SECONDS)
    max_wait = loop.get("max_wait_seconds", DEFAULT_MAX_WAIT_SECONDS)

    return LocalConfig(
        repo=repo,
        checkout=checkout,
        poll_interval_seconds=_require_positive_int(
            interval, "loop.poll_interval_seconds"
        ),
        max_wait_seconds=_require_positive_int(max_wait, "loop.max_wait_seconds"),
    )


def project_orchestrator_config_path(repo: str) -> Path:
    """Per-project local config: ``~/.choir/projects/<owner>/<name>/orchestrator.toml``.

    One overseer machine can run several projects, each with its own checkout
    path and cadence — so the config is namespaced by repo (mirroring how
    ``~/.choir/work/<owner>/<repo>/`` already is), instead of a single shared
    file that the projects would overwrite.
    """
    owner, _, name = repo.partition("/")
    return PROJECTS_DIR / owner / name / "orchestrator.toml"


def read_local_config(
    *, repo: str | None = None, path: Path | None = None
) -> LocalConfig:
    """Read the overseer's local config. Defaults if no file is found.

    Resolution order:

    1. an explicit ``path`` (tests / overrides);
    2. else, if ``repo`` is given, the per-project file
       ``~/.choir/projects/<owner>/<name>/orchestrator.toml`` — falling back to
       the legacy single ``~/.choir/orchestrator.toml`` when the per-project one
       is absent (back-compat);
    3. else the legacy single file.
    """
    if path is not None:
        target = path
    elif repo and "/" in repo:
        per_project = project_orchestrator_config_path(repo)
        target = per_project if per_project.is_file() else ORCHESTRATOR_CONFIG_PATH
    else:
        target = ORCHESTRATOR_CONFIG_PATH
    if not target.is_file():
        return LocalConfig()
    return parse_local_config(target.read_text(encoding="utf-8"))
