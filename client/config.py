"""Contributor-side configuration read from `~/.choir/config.json`.

The file is optional, and everything in it is optional too.
`$CHOIR_CONFIG` overrides the path, mostly for tests.

Schema::

    {
      "tooling": {
        "search": "lean-lsp-mcp" | "agent-provided" | "none"  // optional
      }
    }
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(RuntimeError):
    """A config file exists but its contents are malformed."""


@dataclass
class ToolingConfig:
    search: str | None = None


@dataclass
class Config:
    tooling: ToolingConfig = field(default_factory=ToolingConfig)

    @classmethod
    def default(cls) -> Config:
        return cls(tooling=ToolingConfig())


def config_path() -> Path:
    """Path to the machine-global config."""
    override = os.environ.get("CHOIR_CONFIG")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".choir" / "config.json"


def project_config_path(repo: str) -> Path:
    """Per-project override: ``<.choir>/projects/<owner>/<name>/config.json``.

    Sits beside the global so `$CHOIR_CONFIG` relocates both together.
    """
    owner, _, name = repo.partition("/")
    return config_path().parent / "projects" / owner / name / "config.json"


def _read_json_object(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ConfigError(f"{path}: invalid JSON ({e})") from e
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top-level must be a JSON object")
    return data


def load_config(repo: str | None = None) -> Config:
    """Read contributor config, optionally overlaying a per-project override.

    With no ``repo`` (or none configured), reads only the machine-global
    ``~/.choir/config.json``. When ``repo`` is given and
    ``~/.choir/projects/<owner>/<name>/config.json`` exists, its top-level
    keys overlay the global.
    """
    global_path = config_path()
    data = _read_json_object(global_path)
    source = str(global_path)
    if repo and "/" in repo:
        proj_path = project_config_path(repo)
        proj = _read_json_object(proj_path)
        if proj:
            data = {**data, **proj}  # per-project top-level keys win
            source = f"{global_path} + {proj_path}"
    return _parse_config(data, source)


def _parse_config(data: dict, source: str) -> Config:
    tooling_data = data.get("tooling") or {}
    if not isinstance(tooling_data, dict):
        raise ConfigError(f"{source}: 'tooling' must be an object")
    search = tooling_data.get("search")
    if search is not None and not isinstance(search, str):
        raise ConfigError(f"{source}: 'tooling.search' must be a string")
    return Config(tooling=ToolingConfig(search=search))
