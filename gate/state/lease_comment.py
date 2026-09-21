"""The lease-claim comment format — a fenced ```choir-lease block.

Under spec D4 a contributor has no repository write access, so a claim is a
comment; this is the wire format both `client/` (which writes them) and
`orchestrator/` (which reads them to decide who holds a lease) share.

The block carries single-line scalars only. Its closing fence is the first
```-prefixed line after the opening one — inherited on purpose from the
retired `choir-verdict` block — so this format must never need multi-line
values.

Skew is tolerated in one direction only. An unrecognized *field* must not
fail the parse, or a newer worker could find itself silently unable to
claim on a repo whose gate hasn't upgraded yet. An unrecognized *action*
does fail it: a reader that acts on a verb it does not understand is worse
off than one that ignores the comment.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# The fence info-string. A GitHub-rendered comment shows
# ` ```choir-lease ` as a labeled code block.
LEASE_BLOCK_TAG = "choir-lease"

_FENCE_OPEN = f"```{LEASE_BLOCK_TAG}"

# The closed action vocabulary. Keep in sync with what `client/` writes; a
# verb outside this set is refused rather than guessed at.
ACTION_CLAIM = "claim"
ACTION_HEARTBEAT = "heartbeat"
ACTION_RELEASE = "release"
_VALID_ACTIONS = frozenset({ACTION_CLAIM, ACTION_HEARTBEAT, ACTION_RELEASE})


@dataclass(frozen=True)
class LeaseClaim:
    """One parsed lease-comment claim.

    `action` is one of `ACTION_CLAIM` / `ACTION_HEARTBEAT` /
    `ACTION_RELEASE`; `parse_lease_comment` never returns any other value.

    `session` identifies *which worker session under that login* wrote the
    comment. One login can run several sessions at once, and without this
    the arbiter reads a second session's claim as the holder re-claiming
    its own lease and hands both of them the task. Empty when the writer
    predates the field, which keeps a claim from an older client readable;
    the arbiter falls back to login-only identity for those.

    It carries no authority. `login` is taken from the API row that carried
    the comment, never from the block, so a forged `session` only invents a
    new identity under the forger's own login — which they could do by
    claiming twice anyway.
    """

    login: str
    action: str
    protocol: int
    session: str = ""


def render_lease_comment(claim: LeaseClaim) -> str:
    """Render `claim` as a GitHub comment body: a fenced ```choir-lease block."""
    session = f"session: {claim.session}\n" if claim.session else ""
    return (
        f"```{LEASE_BLOCK_TAG}\n"
        f"login: {claim.login}\n"
        f"action: {claim.action}\n"
        f"protocol: {claim.protocol}\n"
        f"{session}"
        "```\n"
    )


# The block is a few scalar `key: value` lines, so it is parsed by hand
# rather than by a YAML engine. That is a deliberate narrowing of attack
# surface, not a rejection of the house style: `gate/state/intake.py` uses
# `yaml.safe_load` and should, because a task issue body is a rich
# structure. This parser's input arrives from anyone on the internet with no
# membership check, so a parser that understands anchors, aliases and
# recursive merges buys nothing and costs an anchor-expansion exposure
# (`safe_load` is safe against arbitrary *object construction*, not against
# a body that expands to gigabytes). The intake exposure is real but
# pre-existing and wider: narrowing this parser is free, narrowing intake's
# is a schema change.
_MAX_BLOCK_LINES = 32

# A session id as `client.lease` mints it: lowercase hex, long enough that
# two sessions never collide by accident, short enough to read in a thread.
_SESSION_RE = re.compile(r"^[0-9a-f]{8,64}$")
_KEY_RE = re.compile(r"^([a-z][a-z0-9_]*):[ \t]*(.*)$")

# A GitHub login: alphanumeric with single interior hyphens, 1-39 chars.
# Shape-checked rather than merely non-empty because every reader compares
# it against a login from the API. `[1, 2` and `../../etc` are not logins;
# `42` is (numeric usernames are legal), which is why the shape is the
# question and not the type — a hand parser reads every value as a string.
_LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")


def _parse_block_lines(text: str) -> dict[str, str] | None:
    """Parse `key: value` lines into a mapping, or `None` if malformed.

    Bounded at `_MAX_BLOCK_LINES` so an enormous fenced block costs nothing
    to reject. A duplicate key is malformed — an unknown *new* key is
    forward compatibility, but the same key twice is a contradiction, and
    picking a winner would make two readers disagree.
    """
    out: dict[str, str] = {}
    lines = text.splitlines()
    if len(lines) > _MAX_BLOCK_LINES:
        return None
    for line in lines:
        if not line.strip():
            continue
        match = _KEY_RE.match(line.strip())
        if match is None:
            return None
        key, value = match.group(1), match.group(2).strip()
        if key in out:
            return None
        out[key] = value
    return out


def parse_lease_comment(body: str) -> LeaseClaim | None:
    """Parse a GitHub comment body for a lease claim.

    Returns `None` for anything that is not a well-formed lease claim: no
    ```choir-lease fence (the common case, checked as a cheap substring test
    before anything is parsed); an unclosed fence, or a body with a line
    that is not `key: value`, or a repeated key, or more than
    `_MAX_BLOCK_LINES`; a missing `login` / `action` / `protocol`, a
    `protocol` that is not a plain non-negative integer, a `login` that is
    not login-shaped, or an `action` outside `_VALID_ACTIONS`.

    An unrecognized *extra* key is ignored rather than failing the parse —
    that is what keeps a newer client's lease readable by an older reader.

    Never raises: this parses comments posted by anyone on the internet
    under D4, so a malformed or hostile body must degrade to "not a lease"
    and never take down a caller mid-scan of a thread.
    """
    if _FENCE_OPEN not in body:
        return None

    block = _extract_block(body)
    if block is None:
        return None

    data = _parse_block_lines(block)
    if data is None:
        return None

    login = data.get("login")
    if not login or not _LOGIN_RE.match(login):
        return None

    action = data.get("action")
    if action not in _VALID_ACTIONS:
        return None

    raw_protocol = data.get("protocol")
    if raw_protocol is None or not raw_protocol.isdigit():
        return None

    session = data.get("session") or ""
    if session and not _SESSION_RE.match(session):
        # Shape-checked like `login`, and for the same reason: every reader
        # compares it. A value that is not session-shaped is dropped rather
        # than failing the parse, so the claim still arbitrates on its login.
        session = ""

    return LeaseClaim(
        login=login, action=action, protocol=int(raw_protocol), session=session
    )


def _extract_block(body: str) -> str | None:
    """The text between the opening ```choir-lease fence and the next fence
    line, or `None` if the opening fence is never closed.

    The closing fence is the first ```-prefixed line after the opening one,
    whatever its info string; single-line values are what keep that from
    being a trap (see module docstring).
    """
    lines = body.splitlines()

    start: int | None = None
    for i, line in enumerate(lines):
        if line.strip() == _FENCE_OPEN:
            start = i + 1
            break
    if start is None:
        return None

    end = start
    while end < len(lines) and not lines[end].lstrip().startswith("```"):
        end += 1
    if end >= len(lines):
        return None

    return "\n".join(lines[start:end])
