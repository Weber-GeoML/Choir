"""Tests for `client.update` — `choir update`'s subprocess seams (note 13 §6).

Every git/uv call goes through an injectable `runner` (mirrors
`client._subprocess` conventions — `CompletedRun`/`ToolNotFound`), so
these tests never touch a real repo. `choir_checkout_root` is exercised
against a fake tree by monkeypatching `client.__file__`.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

import client
from client._subprocess import CompletedRun, ToolNotFound
from client.update import UpdateError, UpdateResult, choir_checkout_root, run_update


def _make_checkout(tmp_path: Path, *, git_is_file: bool = False) -> Path:
    """A fake Choir checkout tree with `.git` + `gate/protocol.py`, and
    `client/__init__.py` at the location `choir_checkout_root` anchors on."""
    root = tmp_path / "choir-checkout"
    (root / "gate").mkdir(parents=True)
    (root / "gate" / "protocol.py").write_text("PROTOCOL_VERSION = 2\n", encoding="utf-8")
    if git_is_file:
        (root / ".git").write_text("gitdir: /elsewhere/.git/worktrees/x\n", encoding="utf-8")
    else:
        (root / ".git").mkdir()
    client_dir = root / "client"
    client_dir.mkdir()
    (client_dir / "__init__.py").write_text("", encoding="utf-8")
    return root


@pytest.fixture(autouse=True)
def _never_the_real_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`run_update` writes `~/.local/bin/choir`. Autouse so no test in this
    module can install into the developer's HOME by forgetting to say so."""
    monkeypatch.setattr("client.update._entry_point_dir", lambda: tmp_path / "bin")


def _point_client_at(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    monkeypatch.setattr(client, "__file__", str(root / "client" / "__init__.py"))


# ---------------------------------------------------------------------------
# choir_checkout_root
# ---------------------------------------------------------------------------


def test_checkout_root_found_with_git_dir(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    root = _make_checkout(tmp_path)
    _point_client_at(monkeypatch, root)

    assert choir_checkout_root() == root


def test_checkout_root_found_with_git_file_worktree(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    root = _make_checkout(tmp_path, git_is_file=True)
    _point_client_at(monkeypatch, root)

    assert choir_checkout_root() == root


def test_checkout_root_missing_git_raises(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    root = _make_checkout(tmp_path)
    (root / ".git").rmdir()
    _point_client_at(monkeypatch, root)

    with pytest.raises(UpdateError, match="git checkout"):
        choir_checkout_root()


def test_checkout_root_missing_gate_protocol_raises(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    root = _make_checkout(tmp_path)
    (root / "gate" / "protocol.py").unlink()
    _point_client_at(monkeypatch, root)

    with pytest.raises(UpdateError, match=r"gate/protocol\.py"):
        choir_checkout_root()


# ---------------------------------------------------------------------------
# run_update — scripted runner
# ---------------------------------------------------------------------------


def _scripted_runner(
    responses: list[CompletedRun],
) -> tuple[object, list[tuple[list[str], Path]]]:
    """A fake `runner(cmd, cwd)` that returns `responses` in call order."""
    calls: list[tuple[list[str], Path]] = []
    it = iter(responses)

    def runner(cmd: Sequence[str], cwd: Path) -> CompletedRun:
        calls.append((list(cmd), cwd))
        return next(it)

    return runner, calls


def test_run_update_dirty_tree_aborts(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("client.update.choir_checkout_root", lambda: tmp_path)
    runner, calls = _scripted_runner([CompletedRun(0, "M client/cli.py\n", "")])

    with pytest.raises(UpdateError, match="uncommitted"):
        run_update(runner=runner)

    assert len(calls) == 1
    assert calls[0][0] == ["git", "status", "--porcelain"]


def test_run_update_ff_only_failure_aborts(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("client.update.choir_checkout_root", lambda: tmp_path)
    runner, calls = _scripted_runner(
        [
            CompletedRun(0, "", ""),  # git status --porcelain: clean
            CompletedRun(0, "aaaaaaa1111111111111111111111111111111\n", ""),  # old rev
            CompletedRun(1, "", "fatal: Not possible to fast-forward"),  # pull fails
        ]
    )

    with pytest.raises(UpdateError, match="ff-only"):
        run_update(runner=runner)

    assert len(calls) == 3
    assert calls[2][0] == ["git", "pull", "--ff-only"]


def test_run_update_uv_sync_failure_aborts(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("client.update.choir_checkout_root", lambda: tmp_path)
    runner, calls = _scripted_runner(
        [
            CompletedRun(0, "", ""),
            CompletedRun(0, "aaaaaaa1111111111111111111111111111111\n", ""),
            CompletedRun(0, "Updating aaaaaaa..bbbbbbb\n", ""),
            CompletedRun(0, "bbbbbbb2222222222222222222222222222222\n", ""),
            CompletedRun(1, "", "error: failed to resolve dependencies"),  # uv sync fails
        ]
    )

    with pytest.raises(UpdateError, match="uv sync"):
        run_update(runner=runner)

    assert len(calls) == 5
    assert calls[4][0] == ["uv", "sync", "--extra", "dev"]


def test_run_update_success_reports_changed(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("client.update.choir_checkout_root", lambda: tmp_path)
    old_sha = "aaaaaaa1111111111111111111111111111111"
    new_sha = "bbbbbbb2222222222222222222222222222222"
    runner, calls = _scripted_runner(
        [
            CompletedRun(0, "", ""),
            CompletedRun(0, f"{old_sha}\n", ""),
            CompletedRun(0, "Updating aaaaaaa..bbbbbbb\n", ""),
            CompletedRun(0, f"{new_sha}\n", ""),
            CompletedRun(0, "Resolved 12 packages\n", ""),
        ]
    )

    result = run_update(runner=runner)

    shim = tmp_path / "bin" / "choir"
    assert result == UpdateResult(
        old_sha=old_sha, new_sha=new_sha, changed=True, entry_point=str(shim)
    )
    # The point of refreshing it here: a checkout that moved leaves every
    # documented `choir …` pointing at the old path until this runs.
    assert shim.read_text().endswith('exec "$_v" "$@"\n')
    assert shim.stat().st_mode & 0o111
    assert len(calls) == 5
    assert all(cwd == tmp_path for _cmd, cwd in calls)


def test_run_update_already_up_to_date_reports_unchanged(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("client.update.choir_checkout_root", lambda: tmp_path)
    sha = "aaaaaaa1111111111111111111111111111111"
    runner, _calls = _scripted_runner(
        [
            CompletedRun(0, "", ""),
            CompletedRun(0, f"{sha}\n", ""),
            CompletedRun(0, "Already up to date.\n", ""),
            CompletedRun(0, f"{sha}\n", ""),
            CompletedRun(0, "Audited 1 package\n", ""),
        ]
    )

    result = run_update(runner=runner)

    shim = tmp_path / "bin" / "choir"
    assert result == UpdateResult(
        old_sha=sha, new_sha=sha, changed=False, entry_point=str(shim)
    )


def test_run_update_propagates_checkout_root_error(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def boom() -> Path:
        raise UpdateError("not a checkout")

    monkeypatch.setattr("client.update.choir_checkout_root", boom)

    with pytest.raises(UpdateError, match="not a checkout"):
        run_update(runner=lambda cmd, cwd: CompletedRun(0, "", ""))


def test_run_update_tool_not_found_wraps_as_update_error(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("client.update.choir_checkout_root", lambda: tmp_path)

    def boom(cmd: Sequence[str], cwd: Path) -> CompletedRun:
        raise ToolNotFound("git")

    with pytest.raises(UpdateError, match="git"):
        run_update(runner=boom)


def test_run_update_default_runner_uses_subprocess_run(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """Omitting `runner` falls back to the real git/uv subprocess helper."""
    monkeypatch.setattr("client.update.choir_checkout_root", lambda: tmp_path)
    calls = []

    def fake_run(cmd: Sequence[str], *, cwd: Path | None = None, **_kw: object) -> CompletedRun:
        calls.append((list(cmd), cwd))
        if list(cmd)[:2] == ["git", "status"]:
            return CompletedRun(0, "", "")
        if list(cmd) == ["git", "rev-parse", "HEAD"]:
            return CompletedRun(0, "aaaaaaa1111111111111111111111111111111\n", "")
        if list(cmd) == ["git", "pull", "--ff-only"]:
            return CompletedRun(0, "Already up to date.\n", "")
        if list(cmd) == ["uv", "sync", "--extra", "dev"]:
            return CompletedRun(0, "Audited 1 package\n", "")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr("client.update._subprocess_run", fake_run)

    result = run_update()

    assert result.old_sha == "aaaaaaa1111111111111111111111111111111"
    assert result.changed is False
    assert all(cwd == tmp_path for _cmd, cwd in calls)
