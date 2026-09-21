"""Reader for `.choir/verify.toml` — per-project audit policy.

Audits read this config from the **base SHA**, not the PR head. A
contributor can't loosen the audit policy in their own PR (e.g.,
"add Classical.choice to allowed_axioms"); the policy is set by the
project maintainer on the protected branch.

Format::

    [audits.axiom_honesty]
    policy = "net_zero"  # or "whitelist"
    allowed_axioms = ["propext", "Quot.sound", "Classical.choice"]

    [audits.sorry_delta]
    policy = "block"  # or "report"

If the file is absent, all audits use their built-in defaults
(net-zero comparison against base; sorry policy `block`). Missing
sections mean the default for that section.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

VERIFY_CONFIG_RELATIVE_PATH = Path(".choir") / "verify.toml"


class AxiomPolicy(Enum):
    """How `verify-axiom-honesty` decides what counts as a violation."""

    NET_ZERO = "net_zero"
    """Default. Any pattern whose count in head exceeds base flags the PR."""

    WHITELIST = "whitelist"
    """Strict. Axiom *names* not in `allowed_axioms` flag the PR, regardless
    of whether they were already in base. The other watched patterns
    (`unsafe`, `partial`, `native_decide`, `extern`) still use net-zero —
    whitelist only applies to `axiom NAME` declarations."""


@dataclass(frozen=True)
class AxiomHonestyConfig:
    policy: AxiomPolicy = AxiomPolicy.NET_ZERO
    allowed_axioms: tuple[str, ...] = ()


class SorryPolicy(Enum):
    """How `verify-sorry` treats net-new sorries in changed files."""

    BLOCK = "block"
    """Default. Net-new sorries fail the check — final submissions
    must not add unproven obligations."""

    REPORT = "report"
    """Blueprint-style projects: net-new sorries are listed in the
    check output but the check passes. The orchestrator's review
    decides whether the partial progress is worth merging; merged
    sorries land in the inventory as replanning input. The overseer
    accepts that trade by choosing this mode."""


@dataclass(frozen=True)
class SorryDeltaConfig:
    policy: SorryPolicy = SorryPolicy.BLOCK


DEFAULT_STYLE_THRESHOLD_LINES = 200


@dataclass(frozen=True)
class StyleConfig:
    """Max lines per top-level declaration before `verify-style` flags it.

    Generous default (200); domains with genuinely long proofs raise it,
    teaching repos that want tight proofs lower it.
    """

    threshold_lines: int = DEFAULT_STYLE_THRESHOLD_LINES


@dataclass(frozen=True)
class VerifyConfig:
    """Top-level config for all per-project audit policy knobs."""

    axiom_honesty: AxiomHonestyConfig = field(default_factory=AxiomHonestyConfig)
    sorry_delta: SorryDeltaConfig = field(default_factory=SorryDeltaConfig)
    style: StyleConfig = field(default_factory=StyleConfig)


class VerifyConfigError(RuntimeError):
    """Loading or validating `.choir/verify.toml` failed."""


def parse_verify_config(text: str) -> VerifyConfig:
    """Parse `.choir/verify.toml` text into a `VerifyConfig`.

    Empty text or no `[audits]` section returns defaults. Unknown
    sections / fields are ignored (forward-compat). Malformed enum
    values for `policy` raise `VerifyConfigError`.
    """
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise VerifyConfigError(f"malformed TOML — {e}") from e

    audits = data.get("audits", {}) or {}
    if not isinstance(audits, dict):
        raise VerifyConfigError("'audits' section must be a table")

    ah = audits.get("axiom_honesty", {}) or {}
    if not isinstance(ah, dict):
        raise VerifyConfigError("'audits.axiom_honesty' must be a table")

    policy_str = ah.get("policy", AxiomPolicy.NET_ZERO.value)
    try:
        policy = AxiomPolicy(policy_str)
    except ValueError as e:
        valid = ", ".join(p.value for p in AxiomPolicy)
        raise VerifyConfigError(
            f"invalid audits.axiom_honesty.policy = {policy_str!r}; valid: {valid}"
        ) from e

    allowed_raw = ah.get("allowed_axioms", [])
    if not isinstance(allowed_raw, list) or not all(
        isinstance(x, str) for x in allowed_raw
    ):
        raise VerifyConfigError(
            "audits.axiom_honesty.allowed_axioms must be a list of strings"
        )

    sd = audits.get("sorry_delta", {}) or {}
    if not isinstance(sd, dict):
        raise VerifyConfigError("'audits.sorry_delta' must be a table")

    sorry_policy_str = sd.get("policy", SorryPolicy.BLOCK.value)
    try:
        sorry_policy = SorryPolicy(sorry_policy_str)
    except ValueError as e:
        valid = ", ".join(p.value for p in SorryPolicy)
        raise VerifyConfigError(
            f"invalid audits.sorry_delta.policy = {sorry_policy_str!r}; "
            f"valid: {valid}"
        ) from e

    st = audits.get("style", {}) or {}
    if not isinstance(st, dict):
        raise VerifyConfigError("'audits.style' must be a table")
    threshold_raw = st.get("threshold_lines", DEFAULT_STYLE_THRESHOLD_LINES)
    # bool is a subclass of int — reject it explicitly.
    if isinstance(threshold_raw, bool) or not isinstance(threshold_raw, int):
        raise VerifyConfigError(
            f"audits.style.threshold_lines must be an integer; got {threshold_raw!r}"
        )
    if threshold_raw < 1:
        raise VerifyConfigError(
            f"audits.style.threshold_lines must be >= 1; got {threshold_raw}"
        )

    return VerifyConfig(
        axiom_honesty=AxiomHonestyConfig(
            policy=policy,
            allowed_axioms=tuple(allowed_raw),
        ),
        sorry_delta=SorryDeltaConfig(policy=sorry_policy),
        style=StyleConfig(threshold_lines=threshold_raw),
    )


def read_verify_config_from_file(path: Path) -> VerifyConfig:
    """Read `.choir/verify.toml` from `path`. Defaults if file absent."""
    if not path.is_file():
        return VerifyConfig()
    return parse_verify_config(path.read_text(encoding="utf-8"))


def load_verify_config_at_sha(sha: str) -> VerifyConfig:
    """Read `.choir/verify.toml` at `sha` via `git show`.

    Audits call this with the PR's *base* SHA so a contributor can't
    loosen the policy in their own PR — the policy lives on the
    protected branch, where only a maintainer can change it.

    Returns defaults if the file is absent at `sha`, or if it's
    malformed (with a stderr warning — better to fail safe with the
    strict default than to silently bypass).
    """
    result = subprocess.run(
        ["git", "show", f"{sha}:{VERIFY_CONFIG_RELATIVE_PATH}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout:
        return VerifyConfig()
    try:
        return parse_verify_config(result.stdout)
    except VerifyConfigError as e:
        print(
            f"warning: malformed {VERIFY_CONFIG_RELATIVE_PATH} at {sha} "
            f"({e}); using strict defaults",
            file=sys.stderr,
        )
        return VerifyConfig()
