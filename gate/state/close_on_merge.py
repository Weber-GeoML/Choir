"""Identify which issues a merged PR closes, so the workflow can mark them done.

Parses the PR body for `Closes #N` / `Fixes #N` / `Resolves #N` patterns
(and their case variants) — the same keywords GitHub auto-closes on.

A more reliable v1 will query the linked-issues GraphQL API
(`closingIssuesReferences`) so we follow GitHub's own determination
rather than re-implementing it. v0 keeps it as a regex to avoid the
extra API surface; failure mode is conservative (we close fewer issues
than GitHub does, never more).
"""

from __future__ import annotations

import re

_CLOSING_KEYWORDS = (
    "close",
    "closes",
    "closed",
    "fix",
    "fixes",
    "fixed",
    "resolve",
    "resolves",
    "resolved",
)
_PATTERN = re.compile(
    r"(?i)\b(?:" + "|".join(_CLOSING_KEYWORDS) + r")\s+#(\d+)\b",
)


def parse_closing_refs(pr_body: str) -> list[int]:
    """Return the unique issue numbers referenced by closing-keywords in `pr_body`.

    Ordered by first occurrence, deduplicated.
    """
    seen: set[int] = set()
    out: list[int] = []
    for m in _PATTERN.finditer(pr_body):
        n = int(m.group(1))
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out
