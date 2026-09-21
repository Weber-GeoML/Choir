"""Shared PR-changed-file fetch for the delta audit CLIs.

`gh pr view --json files` truncates at 100 changed files — verified
live against a real 248-file PR, which came back with exactly 100.
Every one of the five delta audit CLIs (`statement_immutability_cli`,
`axiom_honesty_cli`, `sorry_delta_cli`, `style_cli`,
`decide_instance_cli`) built its own `fetch_pr_files` on that call, so
all five silently ignored everything past the hundredth changed file:
a PR could pad itself with 100 trivial files and push its real change
out of the audited set entirely. That is a fail-open in five shipped
checks.

This module is the one place that fetches a PR's changed files, using
`gh api --paginate` against the paginated REST list endpoint
(`GET /repos/{owner}/{repo}/pulls/{number}/files`) instead of `gh pr
view`'s non-paginating `--json files`, so every changed file comes
back regardless of how many there are. Base/head SHAs come from a
separate `gh pr view --json baseRefOid,headRefOid` call — that field
never needed pagination; it was only ever capped because it shared one
request with `files`.

Returns exactly the `(base_sha, head_sha, all_changed_file_paths)`
shape every CLI's own local `fetch_pr_files` already returned, so
switching a call site over to this module is a drop-in replacement —
none of the downstream filtering/comparison logic changes.

A `gh` failure raises `PrFilesError` rather than returning a short or
empty file list. That matters as much as the pagination fix itself: a
caller that read "no files" off a failed fetch would print "no
matching source files changed; audit not applicable" and exit 0 — a
truncated list that looks complete, and a failed fetch that looks
empty, are the same fail-open shape wearing different clothes. Raising
is the one response that can't be mistaken for a legitimate result.
"""

from __future__ import annotations

import json
import subprocess


class PrFilesError(RuntimeError):
    """A `gh` invocation while fetching a PR's changed files failed.

    Always raised, never swallowed into an empty or short file list —
    see the module docstring.
    """


def _gh(*args: str) -> str:
    result = subprocess.run(
        ["gh", *args], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise PrFilesError(
            f"gh {' '.join(args)} failed: {(result.stderr or '').strip()}"
        )
    return result.stdout


def _parse_file_entries(raw: str) -> list[dict[str, object]]:
    """Decode `gh api --paginate`'s output into one flat list of entries.

    Current `gh` versions merge same-shaped array pages into a single
    JSON array automatically, so a plain `json.loads` usually suffices
    on its own. `gh api --help` documents a different shape too — each
    page written as its own back-to-back JSON document
    (`[...][...]...`), which is not one JSON document and makes a bare
    `json.loads` raise ("Extra data") on anything past the first page.
    Try the simple parse first; on that specific failure, decode one
    JSON value at a time with `raw_decode` and concatenate the arrays
    ourselves — so this module returns every file regardless of which
    shape the installed `gh` happens to produce.
    """
    text = raw.strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None

    if parsed is not None:
        return list(parsed)

    decoder = json.JSONDecoder()
    items: list[dict[str, object]] = []
    idx = 0
    length = len(text)
    while idx < length:
        page, end = decoder.raw_decode(text, idx)
        items.extend(page)
        idx = end
        while idx < length and text[idx].isspace():
            idx += 1
    return items


def fetch_pr_files(repo: str, pr: int) -> tuple[str, str, list[str]]:
    """Return `(base_sha, head_sha, all_changed_file_paths)` for a PR.

    Unfiltered by extension — callers filter by the effective prover's
    `file_extensions` once the profile is resolved (which itself needs
    `base_sha`, returned here). Every changed file is returned however
    many there are (see module docstring); a `gh` failure raises
    `PrFilesError` rather than returning a short or empty list.
    """
    meta_raw = _gh(
        "pr",
        "view",
        str(pr),
        "--repo",
        repo,
        "--json",
        "baseRefOid,headRefOid",
    )
    meta = json.loads(meta_raw)

    files_raw = _gh(
        "api",
        "--method",
        "GET",
        "--paginate",
        f"repos/{repo}/pulls/{pr}/files",
        "-F",
        "per_page=100",
    )
    entries = _parse_file_entries(files_raw)
    files = [str(entry["filename"]) for entry in entries if entry.get("filename")]
    return meta["baseRefOid"], meta["headRefOid"], files
