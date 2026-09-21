"""Claim-time advisory for Mathlib-search tooling.

A project may recommend Mathlib search (LeanSearch / Loogle / Lean Finder)
via `[tools] mathlib_search` in `.choir/pins.toml`; a contributor declares
what they have via `tooling.search` in `~/.choir/config.json`. This module
reminds a contributor who hasn't declared search when the project recommends
it.

Declare-and-trust: Choir never probes the agent — the search tools are MCP
servers / hosted services, not PATH binaries. Advisory only: this never
raises out of the happy path and never affects an exit code.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from client.config import ConfigError, load_config

PINS_RELATIVE_PATH = Path(".choir") / "pins.toml"

_ADVISORY = (
    "  ⓘ Mathlib search not declared. This project recommends a search "
    "tool\n"
    "    (LeanSearch / Loogle / Lean Finder) — agents prove far better "
    "when they\n"
    "    can search Mathlib instead of guessing lemma names.\n"
    "    Set one up (docs/agents/contributor-setup.md → \"Mathlib search "
    "tooling\"), then declare it\n"
    "    in ~/.choir/config.json:  {\"tooling\": {\"search\": "
    "\"lean-lsp-mcp\"}}\n"
    "    Agent already has search? Use \"agent-provided\". Going "
    "without? \"none\"."
)


def read_tools_recommendation(workspace: Path) -> str:
    """Project policy for Mathlib search: ``"off"`` or ``"recommended"``.

    Reads ``[tools] mathlib_search`` from ``<workspace>/.choir/pins.toml``.
    Anything other than the literal ``"off"`` (missing file/section/key,
    malformed TOML, a non-table ``tools``, a typo'd value) resolves to
    ``"recommended"`` — the advisory fails toward reminding, which is
    harmless. Never raises.
    """
    path = workspace / PINS_RELATIVE_PATH
    if not path.is_file():
        return "recommended"
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError:
        return "recommended"
    tools = data.get("tools", {})
    if not isinstance(tools, dict):
        return "recommended"
    return "off" if tools.get("mathlib_search") == "off" else "recommended"


def tooling_advisory(recommendation: str, search: str | None) -> str | None:
    """Pure decision core: the advisory text, or ``None`` when silent.

    - ``recommendation == "off"`` → ``None`` (project opted out)
    - ``search`` is a non-empty, non-whitespace string → ``None``
      (contributor declared a tool, ``"agent-provided"``, or ``"none"``)
    - otherwise → the advisory text
    """
    if recommendation == "off":
        return None
    if search is not None and search.strip():
        return None
    return _ADVISORY


def mathlib_search_advisory(workspace: Path) -> str | None:
    """Compose the claim-time advisory from project + contributor config.

    Loads ``~/.choir/config.json`` defensively: a malformed config degrades
    to "no search declared" rather than breaking the claim happy path.
    """
    try:
        search = load_config().tooling.search
    except ConfigError:
        search = None
    return tooling_advisory(read_tools_recommendation(workspace), search)
