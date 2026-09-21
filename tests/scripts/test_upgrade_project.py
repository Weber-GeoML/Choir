"""Functional test for `scripts/upgrade-project.sh` (design note 13 §4).

Builds a minimal "degraded old project" fixture in a tmp dir (a plain
`git init` repo — no network, no `gh`), runs the script against it with
`--no-push` and no `--repo`, and asserts the plan's listed outcomes:
overlay files refreshed (incl. `--delete` pruning stale ones), the
protocol pin gets written, `verify-trust-report.yml` appears,
`verify-pr.yml` is left untouched (with a notice printed), exactly one
commit is created, a second run is a no-op, and an unrelated branch +
untracked file are left alone throughout.

The fixture's `gate/`/`orchestrator`/`client` are plausible stand-ins,
not real copies of Choir's packages — the script only moves files
around (rsync) and shells out to the real `gate.upgrade.*` CLIs from
*this* checkout, so the target repo never needs to import anything.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gate.protocol import PROTOCOL_VERSION

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "upgrade-project.sh"

MARKER = "# OVERSEER-CUSTOMIZED-MARKER: do not overwrite\n"


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )


def _run_script(target: Path, *extra_args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), str(target), "--no-push", *extra_args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _build_degraded_fixture(tmp_path: Path) -> Path:
    target = tmp_path / "old-project"
    target.mkdir()

    # Plausible stand-in overlay packages — the script never imports
    # these, it only rsyncs Choir's real trees over them.
    for pkg in ("gate", "orchestrator", "client"):
        (target / pkg).mkdir()
        (target / pkg / "__init__.py").write_text("# stand-in old overlay\n", encoding="utf-8")
    # Simulates a pre-note-13 checkout: no protocol.py yet.
    assert not (target / "gate" / "protocol.py").exists()
    # A stale file no longer present in Choir's current gate/ tree —
    # proves `rsync --delete` actually prunes it.
    stale = target / "gate" / "stale_removed_module.py"
    stale.write_text("# should be pruned by the overlay sync\n", encoding="utf-8")

    (target / "pyproject.toml").write_text("# old pyproject stand-in\n", encoding="utf-8")
    (target / "uv.lock").write_text("# old lock stand-in\n", encoding="utf-8")

    workflows = target / ".github" / "workflows"
    workflows.mkdir(parents=True)
    issue_template = target / ".github" / "ISSUE_TEMPLATE"
    issue_template.mkdir(parents=True)
    (issue_template / "choir-task.md").write_text("old template stand-in\n", encoding="utf-8")
    (workflows / "verify-pr.yml").write_text(MARKER + "name: verify-pr\n", encoding="utf-8")
    # No verify-trust-report.yml yet (pre-note-12.4 bootstrap).

    choir_dir = target / ".choir"
    choir_dir.mkdir()
    # No pin keys yet.
    (choir_dir / "project.toml").write_text('[project]\nprover = "lean4"\n', encoding="utf-8")
    (choir_dir / "verify.toml").write_text(
        "[audits.style]\nthreshold_lines = 200\n", encoding="utf-8"
    )

    (target / "README.md").write_text("# old project\n", encoding="utf-8")

    # What the overseer and the orchestrator author. The overlay is a closed
    # set of gate plumbing, and an upgrade that reached these would discard a
    # project's conventions and everything its orchestrator had learned.
    skills = target / "skills"
    skills.mkdir()
    (skills / "conventions.md").write_text("# overseer's conventions\n", encoding="utf-8")
    (skills / "orchestrator-notes.md").write_text("# learned notes\n", encoding="utf-8")
    roadmap = target / "roadmap"
    roadmap.mkdir()
    (roadmap / "graph.json").write_text('{"nodes": {}}\n', encoding="utf-8")

    _git(["init", "-q", "-b", "main"], target)
    _git(["config", "user.email", "test@example.com"], target)
    _git(["config", "user.name", "Test User"], target)
    _git(["add", "-A"], target)
    _git(["commit", "-q", "-m", "bootstrap (degraded fixture)"], target)

    # An unrelated branch the upgrade must never touch.
    _git(["branch", "other-work"], target)

    # An untracked file the upgrade must never touch or sweep into its
    # commit (also proves the "clean tree" precondition ignores
    # untracked content, not just staged/modified tracked files).
    src_dir = target / "src"
    src_dir.mkdir()
    (src_dir / "scratch.txt").write_text("scratch, not tracked\n", encoding="utf-8")

    return target


def test_upgrade_project_sh_has_valid_bash_syntax() -> None:
    result = subprocess.run(
        ["bash", "-n", str(SCRIPT)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


@pytest.fixture
def fixture_repo(tmp_path: Path) -> Path:
    return _build_degraded_fixture(tmp_path)


def test_upgrade_project_functional(fixture_repo: Path) -> None:
    target = fixture_repo
    other_branch_sha = _git(["rev-parse", "other-work"], target).stdout.strip()
    scratch_path = target / "src" / "scratch.txt"
    scratch_before = scratch_path.read_text(encoding="utf-8")
    log_before = _git(["log", "--oneline"], target).stdout.splitlines()

    result = _run_script(target)
    assert result.returncode == 0, result.stdout + result.stderr

    # 1. Overlay refreshed: gate/protocol.py restored, stale file pruned.
    assert (target / "gate" / "protocol.py").is_file()
    assert not (target / "gate" / "stale_removed_module.py").exists()

    # 2. Pin written with the running Choir's protocol version.
    project_text = (target / ".choir" / "project.toml").read_text(encoding="utf-8")
    assert f"choir_protocol = {PROTOCOL_VERSION}" in project_text
    assert "choir_commit" in project_text

    # 3. verify-trust-report.yml present (lean4 prover).
    assert (target / ".github" / "workflows" / "verify-trust-report.yml").is_file()

    # 4. verify-pr.yml untouched, notice printed.
    verify_pr_text = (target / ".github" / "workflows" / "verify-pr.yml").read_text(
        encoding="utf-8"
    )
    assert MARKER in verify_pr_text
    combined_output = result.stdout + result.stderr
    assert "verify-pr.yml" in combined_output
    assert "force-build-workflow" in combined_output

    # 5. exactly one new commit.
    log_after = _git(["log", "--oneline"], target).stdout.splitlines()
    assert len(log_after) == len(log_before) + 1

    # 6. Everything the project authors is byte-identical.
    assert (target / "skills" / "conventions.md").read_text(encoding="utf-8") == (
        "# overseer's conventions\n"
    )
    assert (target / "skills" / "orchestrator-notes.md").read_text(encoding="utf-8") == (
        "# learned notes\n"
    )
    assert (target / "roadmap" / "graph.json").read_text(encoding="utf-8") == '{"nodes": {}}\n'

    # 7. Unrelated branch + untracked file untouched.
    assert _git(["rev-parse", "other-work"], target).stdout.strip() == other_branch_sha
    assert scratch_path.read_text(encoding="utf-8") == scratch_before
    status = _git(["status", "--porcelain"], target).stdout
    assert "src/scratch.txt" in status or "?? src/" in status

    # 8. Second run: idempotent no-op.
    result2 = _run_script(target)
    assert result2.returncode == 0, result2.stdout + result2.stderr
    assert "already up to date" in (result2.stdout + result2.stderr)
    log_after_second = _git(["log", "--oneline"], target).stdout.splitlines()
    assert log_after_second == log_after
