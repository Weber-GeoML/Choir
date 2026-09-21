"""Shared CLI prover-resolution for the PR-triggered verify audits.

Every delta audit CLI (`axiom_honesty_cli`, `sorry_delta_cli`, `style_cli`,
`statement_equiv_cli`, `decide_instance_cli`) already fetches the PR's base
SHA before doing anything else. This module gives them one shared way to
turn `(--prover flag, base_sha)` into the effective `ProverProfile`, per
design note 12 §2.3's precedence rule:

    --prover flag  >  base SHA's `.choir/project.toml`  >  lean4 default

Reading from the *base* SHA (not the PR head) is the same discipline as
the axiom/sorry/style policy readers in `gate.verify.config` — a
contributor's own PR can't switch provers to dodge whichever audits apply.
"""

from __future__ import annotations

from pathlib import Path

from gate.provers import ProverProfile, get_profile
from gate.provers.select import read_prover_at_sha


def resolve_prover_profile(
    flag: str | None, base_sha: str, *, cwd: Path | None = None
) -> ProverProfile:
    """Resolve the effective `ProverProfile` for a base-SHA-aware audit CLI.

    `flag` is the CLI's `--prover` argument (`None` if not given). Falls
    back to `read_prover_at_sha(base_sha)`, which itself defaults to
    `"lean4"` when `.choir/project.toml` is absent at that SHA. Raises
    `gate.provers.ProverError` for an unknown prover name, whether it came
    from the flag or from the config.
    """
    name = flag if flag is not None else read_prover_at_sha(base_sha, cwd=cwd)
    return get_profile(name)


def filter_files_by_profile(files: list[str], profile: ProverProfile) -> list[str]:
    """Keep only the paths ending in one of the profile's file extensions.

    Shared by every delta audit CLI (`axiom_honesty_cli`, `sorry_delta_cli`,
    `style_cli`, ...) — was verbatim-duplicated in each before being
    consolidated here.
    """
    extensions = tuple(profile.file_extensions)
    return [f for f in files if f.endswith(extensions)]
