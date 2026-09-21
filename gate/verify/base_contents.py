"""Shared git-content-fetching helpers for the PR delta-audit CLIs.

`detect_rename` and `fetch_base_contents` used to live only in
`gate/verify/statement_equiv_cli.py`. They exist to close the "bug-#5"
bypass: `git show base_sha:head_path` fails silently for a file that
was renamed within the PR (its blob lives under the *old* path at
`base_sha`, not the new one), so a naive base-content fetch reads a
renamed file as brand new — with zero base declarations — even though
the file (under its old name) already had declarations at base. A
worker could rename a file and rewrite everything in it and have that
read as legitimately new content.

Extracted here (verbatim logic, byte-identical behaviour) so that a
second audit CLI needing the same rename-aware base lookup — spec D2's
`statement_immutability_cli.py`, which must apply this per changed
file rather than to one named target — imports it rather than
maintaining a third hand-written copy. `gate/checks.py`'s check
registry exists precisely because two hand-maintained copies of one
fact had already drifted apart and shipped a bug; a security-relevant
helper like this one is exactly the kind of fact that must not be
copied a third time.

This module has its own `_git` and `_ExternalError` rather than
importing either from `statement_equiv_cli` — callers here never see
`_ExternalError` (both public functions catch it internally and
degrade to `""`/`None`), so there is nothing to keep in sync with any
particular CLI's own external-error type.
"""

from __future__ import annotations

import subprocess


class _ExternalError(RuntimeError):
    pass


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise _ExternalError(
            f"git {' '.join(args)} failed: {(result.stderr or '').strip()}"
        )
    return result.stdout


def detect_rename(base_sha: str, head_sha: str, head_path: str) -> str | None:
    """Return the file's old name at `base_sha` if it was renamed in the PR.

    Uses `git log --follow --diff-filter=R --name-status` to find rename
    commits in the [base_sha, head_sha) range whose new-name matches
    `head_path`. Returns the old name, or None if no rename is found.

    This closes the bug-#5 bypass where a contributor could rename the
    target_file in the PR. Before this lookup, `git show base_sha:head_path`
    failed silently → empty base content → statement_equiv verdict was
    UNDETERMINED (passed). With the lookup, we find the old name and
    extract the base signature correctly.
    """
    try:
        out = _git(
            "log",
            f"{base_sha}..{head_sha}",
            "--follow",
            "--diff-filter=R",
            "--name-status",
            "--pretty=format:",
            "--",
            head_path,
        )
    except _ExternalError:
        return None

    # Output lines for rename entries look like:
    #     R100\tOldName.lean\tNewName.lean
    # (R followed by a similarity score, then old and new paths).
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and parts[0].startswith("R") and parts[2] == head_path:
            return parts[1]
    return None


def fetch_base_contents(base_sha: str, head_sha: str, target_file: str) -> tuple[str, str | None]:
    """Return `(base_contents, renamed_from)` for the target file.

    Tries `git show base_sha:target_file` directly first; on failure,
    looks for a rename in the PR's history and retries with the old
    name. `renamed_from` is the old name when a rename was applied,
    else None.
    """
    try:
        return _git("show", f"{base_sha}:{target_file}"), None
    except _ExternalError:
        pass

    old_name = detect_rename(base_sha, head_sha, target_file)
    if old_name is None:
        return "", None
    try:
        return _git("show", f"{base_sha}:{old_name}"), old_name
    except _ExternalError:
        return "", old_name
