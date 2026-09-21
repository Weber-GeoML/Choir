"""Reader for the `[reconcile]` section of `.choir/project.toml`.

The reconcile workflow runs daily and reclaims claims whose heartbeat
(or last activity) is older than `stale_after_days`. Projects tune this:
a fast benchmark wants a *short* window so an abandoned problem recycles
quickly; a months-long formalization wants a *long* one so a contributor
mid-proof isn't reclaimed out from under them.

Unlike the verify audits, reconcile is a *scheduled* job, not a
PR-triggered one — there is no base SHA and no self-loosening concern, so
the value is read from the default branch's checkout (HEAD), not from a
PR base. `.choir/project.toml` therefore has two readers: the orchestrator
reads `[automation]` (see `orchestrator/project_config.py`), the gate reads
`[reconcile]` here. Keeping a separate gate-side reader preserves the
layering rule — the gate never imports the orchestrator package.

Format::

    [reconcile]
    stale_after_days = 7

Absent file / section / key → the built-in default (7). An invalid value
raises `ReconcileConfigError` so a typo doesn't silently revert to the
default; the CLI catches it and warns rather than crashing the daily job.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

PROJECT_CONFIG_RELATIVE_PATH = Path(".choir") / "project.toml"
DEFAULT_STALE_AFTER_DAYS = 7


@dataclass(frozen=True)
class ReconcileConfig:
    stale_after_days: int = DEFAULT_STALE_AFTER_DAYS


class ReconcileConfigError(RuntimeError):
    """Loading or validating the `[reconcile]` section failed."""


def parse_reconcile_config(text: str) -> ReconcileConfig:
    """Parse `.choir/project.toml` text, taking only the `[reconcile]` section.

    Empty text / missing section / missing key return the default.
    Unknown sections and keys are ignored (forward-compat — `[automation]`
    lives in the same file and must not trip this reader). A non-integer
    or out-of-range `stale_after_days` raises.
    """
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ReconcileConfigError(f"malformed TOML — {e}") from e

    section = data.get("reconcile", {}) or {}
    if not isinstance(section, dict):
        raise ReconcileConfigError("'reconcile' section must be a table")

    raw = section.get("stale_after_days", DEFAULT_STALE_AFTER_DAYS)
    # bool is a subclass of int — reject it explicitly so `= true` isn't 1.
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ReconcileConfigError(
            f"reconcile.stale_after_days must be an integer; got {raw!r}"
        )
    if raw < 1:
        raise ReconcileConfigError(
            f"reconcile.stale_after_days must be >= 1; got {raw}"
        )
    return ReconcileConfig(stale_after_days=raw)


def read_reconcile_config(checkout: Path) -> ReconcileConfig:
    """Read `[reconcile]` from `.choir/project.toml` under `checkout`.

    Defaults if the file is absent.
    """
    path = checkout / PROJECT_CONFIG_RELATIVE_PATH
    if not path.is_file():
        return ReconcileConfig()
    return parse_reconcile_config(path.read_text(encoding="utf-8"))
