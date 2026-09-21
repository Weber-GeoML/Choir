"""Tests for `gate.verify.pr_files.fetch_pr_files`.

`gh pr view --json files` truncates at 100 changed files (verified
live against a real 248-file PR); every one of the five delta audit
CLIs used to build its own `fetch_pr_files` on that call and silently
drop everything past the hundredth file. This module replaces all five
with one shared fetch built on `gh api --paginate` against the REST
list endpoint instead, which does not truncate.

These tests pin down the two properties that matter:

- every changed file comes back, however many there are (a PR padded
  with more than 100 trivial files must not push its real change out
  of the audited set);
- a `gh` failure raises `PrFilesError` rather than returning a short
  or empty list — a truncated-looking-complete list and a
  failed-looking-empty list are the same fail-open shape, and an
  audit that reads "no files" off a failed fetch prints "not
  applicable" and exits 0.

No network/subprocess is touched: `subprocess.run` is monkeypatched to
a fake that inspects the `gh` invocation and returns a canned
`CompletedProcess`-shaped result, mirroring the pattern in
`tests/orchestrator/test_prs.py`.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

import pytest

from gate.verify.pr_files import PrFilesError, _parse_file_entries, fetch_pr_files


@dataclass
class _FakeProc:
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0


def _view_response(base_sha: str = "b" * 40, head_sha: str = "h" * 40) -> str:
    return json.dumps({"baseRefOid": base_sha, "headRefOid": head_sha})


def _files_page(filenames: list[str]) -> str:
    return json.dumps([{"filename": name} for name in filenames])


def test_fetch_pr_files_returns_more_than_100_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The old `gh pr view --json files` capped out at 100. A PR with
    more must still return every one of them, not just the first 100."""
    filenames = [f"Sample/File{i}.lean" for i in range(150)]

    def _fake_run(cmd: list[str], **kwargs: object) -> _FakeProc:
        if cmd[:3] == ["gh", "pr", "view"]:
            return _FakeProc(stdout=_view_response())
        if cmd[:2] == ["gh", "api"]:
            return _FakeProc(stdout=_files_page(filenames))
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    base_sha, head_sha, files = fetch_pr_files("o/r", 42)

    assert base_sha == "b" * 40
    assert head_sha == "h" * 40
    assert len(files) == 150
    assert files == filenames


def test_fetch_pr_files_raises_on_gh_view_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `gh` failure must raise, never collapse into an empty or
    short file list — that would look exactly like "no files changed,"
    which every caller reads as "audit not applicable" and exits 0."""

    def _fake_run(cmd: list[str], **kwargs: object) -> _FakeProc:
        if cmd[:3] == ["gh", "pr", "view"]:
            return _FakeProc(returncode=1, stderr="gh: pull request not found")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    with pytest.raises(PrFilesError, match="not found"):
        fetch_pr_files("o/r", 42)


def test_fetch_pr_files_raises_on_gh_api_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same property, at the paginated files fetch rather than the
    metadata fetch: a failure there must also raise, not return `[]`
    (which would read as a legitimate zero-file PR)."""

    def _fake_run(cmd: list[str], **kwargs: object) -> _FakeProc:
        if cmd[:3] == ["gh", "pr", "view"]:
            return _FakeProc(stdout=_view_response())
        if cmd[:2] == ["gh", "api"]:
            return _FakeProc(returncode=1, stderr="gh: rate limit exceeded")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    with pytest.raises(PrFilesError, match="rate limit"):
        fetch_pr_files("o/r", 42)


def test_fetch_pr_files_skips_entries_without_filename(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defensive: an entry missing/empty `filename` (shouldn't happen
    against the real API, but mirrors the `f.get("path")` guard the
    old per-CLI implementations all had) is dropped rather than
    crashing or producing a bogus empty path."""

    def _fake_run(cmd: list[str], **kwargs: object) -> _FakeProc:
        if cmd[:3] == ["gh", "pr", "view"]:
            return _FakeProc(stdout=_view_response())
        if cmd[:2] == ["gh", "api"]:
            return _FakeProc(
                stdout=json.dumps(
                    [{"filename": "A.lean"}, {"filename": ""}, {"other": "x"}]
                )
            )
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    _base, _head, files = fetch_pr_files("o/r", 42)
    assert files == ["A.lean"]


# ---------------------------------------------------------------------------
# _parse_file_entries — both shapes `gh api --paginate` can produce
# ---------------------------------------------------------------------------


def test_parse_file_entries_single_merged_array() -> None:
    """Current `gh` versions merge same-shaped array pages into one
    JSON array; a plain `json.loads` handles this directly."""
    raw = json.dumps([{"filename": "A.lean"}, {"filename": "B.lean"}])
    assert _parse_file_entries(raw) == [
        {"filename": "A.lean"},
        {"filename": "B.lean"},
    ]


def test_parse_file_entries_concatenated_page_documents() -> None:
    """`gh api --help` documents an alternate shape: each page written
    as its own back-to-back JSON document (`[...][...]`), which is not
    one JSON document. Both pages' entries must still come back."""
    page1 = json.dumps([{"filename": "A.lean"}])
    page2 = json.dumps([{"filename": "B.lean"}, {"filename": "C.lean"}])
    raw = page1 + page2
    assert _parse_file_entries(raw) == [
        {"filename": "A.lean"},
        {"filename": "B.lean"},
        {"filename": "C.lean"},
    ]


def test_parse_file_entries_empty_string() -> None:
    assert _parse_file_entries("") == []
    assert _parse_file_entries("   ") == []
