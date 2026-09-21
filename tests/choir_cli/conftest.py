"""Isolation for purge tests.

Purge deletes directories. Every root it can reach must be redirected into
`tmp_path` before any test runs — a purge test that escapes into the
developer's real `~/.choir` deletes their work.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_choir_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "choir-home"
    home.mkdir()
    monkeypatch.setenv("CHOIR_CONFIG", str(home / "config.json"))
    monkeypatch.setenv("CHOIR_WORK_ROOT", str(home / "work"))
    monkeypatch.setenv("CHOIR_REPO_STORE", str(home / "repo-store"))
    monkeypatch.setenv("CHOIR_BUILD_STORE", str(home / "build-store"))
    monkeypatch.setenv("CHOIR_MATHLIB_STORE", str(home / "mathlib-store"))
