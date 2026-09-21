"""Tests for the `gate.upgrade.manifest` CLI — the side-effectful half.

Exercised through `main()` against a real temporary git repo, because the
seed-mode notice reads `git ls-files` and the delete path touches the
filesystem.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from gate.upgrade.manifest import MANIFEST_RELPATH, main

WF = ".github/workflows"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / WF).mkdir(parents=True)
    (root / ".choir").mkdir()
    subprocess.run(
        ["git", "init", "-q", str(root)], check=True, capture_output=True
    )
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    return root


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _manifest(root: Path) -> dict:
    return json.loads((root / MANIFEST_RELPATH).read_text(encoding="utf-8"))


def test_seed_run_writes_a_manifest_and_deletes_nothing(repo: Path, capsys) -> None:
    _write(repo, f"{WF}/verify-style.yml", "style\n")
    _write(repo, f"{WF}/verify-review.yml", "orphan\n")
    _git(repo, "add", "-A")

    rc = main([str(repo), "--managed", f"{WF}/verify-style.yml"])

    assert rc == 0
    assert (repo / WF / "verify-review.yml").exists()
    assert set(_manifest(repo)["files"]) == {f"{WF}/verify-style.yml"}
    out = capsys.readouterr().out
    assert f"unmanaged\t{WF}/verify-review.yml" in out
    assert "deleted" not in out


def test_second_run_deletes_a_file_dropped_from_the_overlay_set(repo: Path, capsys) -> None:
    _write(repo, f"{WF}/verify-style.yml", "style\n")
    _write(repo, f"{WF}/verify-review.yml", "review\n")
    _git(repo, "add", "-A")
    main(
        [
            str(repo),
            "--managed",
            f"{WF}/verify-style.yml",
            "--managed",
            f"{WF}/verify-review.yml",
        ]
    )
    capsys.readouterr()

    rc = main([str(repo), "--managed", f"{WF}/verify-style.yml"])

    assert rc == 0
    assert not (repo / WF / "verify-review.yml").exists()
    assert set(_manifest(repo)["files"]) == {f"{WF}/verify-style.yml"}
    assert f"deleted\t{WF}/verify-review.yml" in capsys.readouterr().out


def test_locally_edited_managed_file_survives_retirement(repo: Path, capsys) -> None:
    _write(repo, f"{WF}/verify-review.yml", "review\n")
    _git(repo, "add", "-A")
    main([str(repo), "--managed", f"{WF}/verify-review.yml"])
    capsys.readouterr()
    _write(repo, f"{WF}/verify-review.yml", "review, but I changed it\n")

    rc = main([str(repo)])

    assert rc == 0
    assert (repo / WF / "verify-review.yml").exists()
    out = capsys.readouterr().out
    assert f"kept\t{WF}/verify-review.yml" in out
    assert "edited locally" in out


def test_seeded_file_survives_retirement(repo: Path, capsys) -> None:
    _write(repo, ".choir/review.toml", "[review]\n")
    _git(repo, "add", "-A")
    main([str(repo), "--seeded", ".choir/review.toml"])
    capsys.readouterr()

    rc = main([str(repo)])

    assert rc == 0
    assert (repo / ".choir" / "review.toml").exists()
    assert "yours to remove" in capsys.readouterr().out


def test_retired_path_already_removed_by_hand_is_silent(repo: Path, capsys) -> None:
    _write(repo, f"{WF}/verify-review.yml", "review\n")
    _git(repo, "add", "-A")
    main([str(repo), "--managed", f"{WF}/verify-review.yml"])
    capsys.readouterr()
    (repo / WF / "verify-review.yml").unlink()

    rc = main([str(repo)])

    assert rc == 0
    out = capsys.readouterr().out
    assert out == ""


def test_unmanaged_notice_is_silent_after_the_seed_run(repo: Path, capsys) -> None:
    _write(repo, f"{WF}/verify-style.yml", "style\n")
    _write(repo, f"{WF}/mine.yml", "mine\n")
    _git(repo, "add", "-A")
    main([str(repo), "--managed", f"{WF}/verify-style.yml"])
    capsys.readouterr()

    main([str(repo), "--managed", f"{WF}/verify-style.yml"])

    assert "unmanaged" not in capsys.readouterr().out


def test_a_file_no_manifest_claims_is_never_touched(repo: Path) -> None:
    """The fork property: only this lineage's own installs are retirable."""
    _write(repo, f"{WF}/verify-style.yml", "style\n")
    _write(repo, f"{WF}/my-fork-check.yml", "mine\n")
    _git(repo, "add", "-A")
    main([str(repo), "--managed", f"{WF}/verify-style.yml"])
    main([str(repo)])
    assert (repo / WF / "my-fork-check.yml").exists()


def test_corrupt_manifest_refuses_and_changes_nothing(repo: Path) -> None:
    _write(repo, f"{WF}/verify-review.yml", "review\n")
    _write(repo, MANIFEST_RELPATH, "{not json")
    _git(repo, "add", "-A")

    rc = main([str(repo), "--managed", f"{WF}/verify-style.yml"])

    assert rc == 2
    assert (repo / WF / "verify-review.yml").exists()
    assert (repo / MANIFEST_RELPATH).read_text(encoding="utf-8") == "{not json"


def test_unsafe_overlay_argument_is_refused(repo: Path) -> None:
    assert main([str(repo), "--managed", "../../etc/passwd"]) == 2
    assert not (repo / MANIFEST_RELPATH).exists()


def test_repeated_run_produces_a_byte_identical_manifest(repo: Path) -> None:
    _write(repo, f"{WF}/verify-style.yml", "style\n")
    _git(repo, "add", "-A")
    main([str(repo), "--managed", f"{WF}/verify-style.yml"])
    first = (repo / MANIFEST_RELPATH).read_text(encoding="utf-8")
    main([str(repo), "--managed", f"{WF}/verify-style.yml"])
    assert (repo / MANIFEST_RELPATH).read_text(encoding="utf-8") == first


def test_declared_path_absent_on_disk_is_omitted_from_the_manifest(repo: Path) -> None:
    """A prover-conditional file must not enter the manifest as a phantom, or
    the next run would read it as retired."""
    rc = main([str(repo), "--managed", f"{WF}/verify-trust-report.yml"])
    assert rc == 0
    assert _manifest(repo)["files"] == {}
