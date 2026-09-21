"""Shared client-test isolation.

`client.worker.run()` reads the real contributor config (and, under
a stray real config would otherwise leak into tests and
`os.execv`) unless the config path is redirected. Tests must never be
coupled to the developer's ambient `~/.choir/config.json` — a
contributor with a real config should be able to run the
suite without pytest self-updating out from under them (P3 Task 4
review finding).

Individual tests that need a specific config still win: a test-level
`monkeypatch.setenv("CHOIR_CONFIG", ...)` runs after this autouse
fixture and overrides it for that test.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_choir_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "CHOIR_CONFIG", str(tmp_path / "isolated-nonexistent-config.json")
    )


@pytest.fixture(autouse=True)
def _scrub_worker_updated_flag() -> Iterator[None]:
    # Production code sets CHOIR_WORKER_UPDATED via os.environ directly
    # (before re-exec), which monkeypatch does not restore — scrub it on
    # both sides of every test so no test leaks the flag into the next.
    os.environ.pop("CHOIR_WORKER_UPDATED", None)
    yield
    os.environ.pop("CHOIR_WORKER_UPDATED", None)
