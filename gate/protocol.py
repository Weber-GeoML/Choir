"""`PROTOCOL_VERSION` — the repo-pinned wire-protocol version (design note 13).

Consistency between overseer, workers, and gate is anchored to a single
integer, not to code-commit equality (note 13 §1-2): it bumps when a
change alters what parties must agree on — TaskRecord fields/semantics,
lifecycle/priority/difficulty label vocabulary, `.choir/*` config
schemas, lease/branch conventions, the workflow<->CLI contract. It does
NOT bump for internal refactors, docs, client-only features, or
informational audits.

- **1** = the pre-note-11 wire (retroactive; any repo without a pin).
- **2** = priority/difficulty labels, review.toml v2 + rubric packs,
  `type: review` tasks with `target_pr`, choir-verdict comments,
  `[project] prover`, verify-trust-report.
- **3** = adds `[review.round1] allow_self_review` to review.toml.
- **4** = adds the `review` required status check (verify-review): a
  recorded choir-verdict block became a merge precondition.
- **5** = `TaskType` drops `formalize`/`draft`/`refactor` — removing enum
  members is not backward-compatible, since a protocol-4 repo holding
  one of those tasks would fail intake under this code. The same bump
  carries spec D1's removal of the review layer (`verify-review`,
  round-1 `type: review` tasks, `.choir/review.toml`, the choir-verdict
  block, the rubric packs) and the addition of `comparator` to the
  branch-protection required contexts.
- **6** = adds the `statement-immutability` required status check (spec
  D2). An orchestrator on 6 against a repo pinned at 5 refuses every PR,
  because the workflow is absent there so the check reads *absent*
  rather than failing. 6 was also the number of an earlier unreleased
  attempt; nothing was ever pushed at it (note 13 §2).
- **7** = the lease is a comment (spec D4). A claim is a fenced
  ```choir-lease block (`gate/state/lease_comment.py`) and the holder is
  the earliest claim comment by GitHub's monotonic comment id
  (`gate/state/lease_arbiter.py`); self-assignment and the
  worker-written lifecycle/heartbeat labels are gone, since a
  contributor under open contribution has no write access for them.

The pin lives in `.choir/project.toml`::

    [project]
    choir_protocol = 2          # written by bootstrap + upgrade, never by hand
    choir_commit = "<sha>"      # provenance only

Readers here are gate-side and deliberately never raise: the pin is
advisory metadata for the gate, and the hard minimum-version gate is
enforced client-side (note 13 §6), reusing `parse_protocol_pin` on file
text fetched over the GitHub API. Absent file/section/key, malformed
TOML, or a non-int value all read as protocol **1** — the safest
default, since every pre-pin repo is protocol 1.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

PROTOCOL_VERSION = 8

PROJECT_CONFIG_RELATIVE_PATH = Path(".choir") / "project.toml"
DEFAULT_PROTOCOL_PIN = 1


def parse_protocol_pin(text: str) -> int:
    """Parse `.choir/project.toml` text, taking only `[project].choir_protocol`.

    Never raises: empty text, missing section/key, malformed TOML, or a
    non-int value (including `bool`, which is an `int` subclass in
    Python) all default to `DEFAULT_PROTOCOL_PIN` (1).
    """
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return DEFAULT_PROTOCOL_PIN

    section = data.get("project", {})
    if not isinstance(section, dict):
        return DEFAULT_PROTOCOL_PIN

    raw = section.get("choir_protocol", DEFAULT_PROTOCOL_PIN)
    if isinstance(raw, bool) or not isinstance(raw, int):
        return DEFAULT_PROTOCOL_PIN
    return raw


def read_protocol_pin(workspace: Path) -> int:
    """Read the protocol pin from `.choir/project.toml` under `workspace`.

    Defaults to `DEFAULT_PROTOCOL_PIN` (1) if the file is absent — never
    raises (see module docstring).
    """
    path = workspace / PROJECT_CONFIG_RELATIVE_PATH
    if not path.is_file():
        return DEFAULT_PROTOCOL_PIN
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # UnicodeDecodeError is a ValueError, not an OSError, so it needs
        # naming here for a binary project.toml to read as "unpinned".
        return DEFAULT_PROTOCOL_PIN
    return parse_protocol_pin(text)
