"""ReductionRecord model — the declared half of a reduction submission.

A reduction proves its target modulo named open obligations. The block
names them, which is what scopes the gate's placeholder relaxation to a
submission that asked for it: absent or malformed, the relaxation does
not apply.

The block is a fenced `choir-reduction` region in the PR body rather
than front matter, so it composes with whatever else the body says.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

REDUCTION_BLOCK_TAG = "choir-reduction"
"""Fence language marking the block in a PR body."""

_DECL_RE = re.compile(r"^[^\W\d][\w']*(?:\.[^\W\d][\w']*)*$")
"""A declaration name, dot-qualified segment by segment.

Each segment between dots must itself be an identifier: start with a
letter or underscore (not a digit), continue with letters, digits,
underscores, or apostrophes. A leading, trailing, or doubled dot is
rejected rather than accepted into the name — a `parent` or `decl`
ending in a stray dot would otherwise pass here and then split to an
empty leaf downstream, where a degenerate leaf must be caught for the
check that reads it to fail closed rather than silently matching
nothing.

`\\w` is Unicode-aware by default for a `str` pattern (no `re.ASCII`
here), so a Lean identifier written with Greek letters, subscripts, or
other non-ASCII letters is a valid declaration name, matching what the
prover itself accepts.
"""

_BLOCK_RE = re.compile(
    rf"^(?P<indent>[ \t]*)```[ \t]*{REDUCTION_BLOCK_TAG}[ \t]*$\n"
    rf"(?P<yaml>.*?)^(?P=indent)```[ \t]*$",
    re.MULTILINE | re.DOTALL,
)
_OPENER_RE = re.compile(
    rf"^[ \t]*```[ \t]*{REDUCTION_BLOCK_TAG}[ \t]*$", re.MULTILINE
)


@dataclass(frozen=True)
class ReductionParseError:
    """A block was present but unusable. Grants no relaxation."""

    message: str


class ReductionChild(BaseModel):
    """One open obligation the submission leans on.

    `decl` may name a declaration this PR adds or one already in the
    corpus; both are obligations from the parent's point of view.
    `blueprint_ref` and `note` are review input and are not verified.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    decl: str
    blueprint_ref: str | None = None
    note: str | None = None

    @field_validator("decl")
    @classmethod
    def _check_decl(cls, v: str) -> str:
        if not _DECL_RE.match(v):
            raise ValueError("decl must be a declaration name")
        return v


class ReductionRecord(BaseModel):
    """A v1 reduction block."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        populate_by_name=True,
    )

    choir_reduction_version: Literal[1] = Field(alias="choir-reduction-version")
    parent: str
    children: list[ReductionChild] = Field(min_length=1)

    @field_validator("parent")
    @classmethod
    def _check_parent(cls, v: str) -> str:
        if not _DECL_RE.match(v):
            raise ValueError("parent must be a declaration name")
        return v


def parse_reduction_block(
    body: str,
) -> ReductionRecord | ReductionParseError | None:
    """Extract and validate the reduction block in `body`.

    `None` means no block is present, which is the ordinary proof
    submission. A `ReductionParseError` means one was present and could
    not be used — reported to the contributor, and granting nothing.
    """
    match = _BLOCK_RE.search(body)
    if match is None:
        if _OPENER_RE.search(body):
            return ReductionParseError(
                message=(
                    f"a '{REDUCTION_BLOCK_TAG}' block was opened but not "
                    "properly closed (the closing '```' must sit at the "
                    "same indentation as the opener)"
                )
            )
        return None

    try:
        data = yaml.safe_load(match.group("yaml"))
    except yaml.YAMLError as e:
        return ReductionParseError(message=f"block is not valid YAML: {e}")

    if not isinstance(data, dict):
        return ReductionParseError(
            message="block must be a YAML mapping of fields"
        )

    try:
        return ReductionRecord.model_validate(data)
    except ValidationError as e:
        details = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
            for err in e.errors()
        )
        return ReductionParseError(message=details)
