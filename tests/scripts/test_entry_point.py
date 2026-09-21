"""`choir` on PATH after setup, from both setup scripts.

Every command in `docs/agents/ORCHESTRATOR.md` and
`docs/agents/CONTRIBUTOR.md` is written as a bare `choir …`, so setup has
to leave one there. These tests run each script against a fixture
checkout — never the real one, since the repair path deletes `.venv` —
with a fake HOME and every tool setup installs stubbed out — nothing
here touches the network — and assert the boundary: a `choir` that runs,
or a flagged reason why not.

`install_entry_point` is duplicated between the two scripts, like the
workflow heredocs in `test_template_parity.py` — both are standalone
`sh` a user runs before there is anything to source from. The parity
test is what keeps the copies honest.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from client.update import entry_point_script

REPO_ROOT = Path(__file__).resolve().parents[2]
ORCH_INIT = REPO_ROOT / "scripts" / "orchestrator-init.sh"
JOIN = REPO_ROOT / "scripts" / "join.sh"

STUB_MARKER = "STUB-CHOIR-RAN"


def _build_fixture(tmp_path: Path, script: Path) -> tuple[Path, Path, Path, Path]:
    """A fake checkout, a fake HOME, a working folder, and tool stubs.

    The script derives CHOIR_DIR from its own path, so copying it under
    `choir/scripts/` is what keeps every mutating path (`uv sync`, the
    `.venv` rebuild) inside the fixture. `work` is the other half: both
    scripts write `AGENTS.md` and `CLAUDE.md` into the folder they are run
    from, so the run needs a cwd of its own or it writes into whichever
    directory the suite happened to start in.
    """
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)

    work = tmp_path / "work"
    work.mkdir()

    choir = tmp_path / "choir"
    (choir / "scripts").mkdir(parents=True)
    (choir / "scripts" / script.name).write_text(script.read_text(), encoding="utf-8")
    (choir / "pyproject.toml").write_text('[project]\nname = "choir"\n', encoding="utf-8")

    venv_bin = choir / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    real_choir = venv_bin / "choir"
    real_choir.write_text(f'#!/bin/sh\necho "{STUB_MARKER} $*"\n', encoding="utf-8")
    real_choir.chmod(0o755)

    stubs = tmp_path / "stubs"
    stubs.mkdir()
    # `gh repo view` succeeding with push rights takes the resume branch;
    # `uv` is a no-op so no network and no real environment is built.
    (stubs / "gh").write_text(
        '#!/bin/sh\ncase "$1" in\n'
        '  auth) exit 0 ;;\n'
        '  repo) exit 0 ;;\n'
        '  api) echo true ;;\n'
        'esac\nexit 0\n',
        encoding="utf-8",
    )
    (stubs / "uv").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    # `join.sh` installs elan when it misses, so an unstubbed fixture runs
    # the real `curl … elan-init.sh | sh` against the fake HOME. `curl`
    # fails loudly rather than being absent, so any future network path
    # shows up as a failure here instead of a download.
    (stubs / "elan").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (stubs / "curl").write_text(
        '#!/bin/sh\necho "test stub: no network in tests" >&2\nexit 1\n', encoding="utf-8"
    )
    for name in ("gh", "uv", "elan", "curl"):
        (stubs / name).chmod(0o755)

    return home, choir, stubs, work


def _run(script: Path, tmp_path: Path, *, bin_on_path: bool) -> subprocess.CompletedProcess[str]:
    home, choir, stubs, work = _build_fixture(tmp_path, script)
    path = f"{stubs}:/usr/bin:/bin"
    if bin_on_path:
        path = f"{home / '.local' / 'bin'}:{path}"
    return subprocess.run(
        ["sh", str(choir / "scripts" / script.name), "org/project"],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(work),
        env={"HOME": str(home), "PATH": path},
    )


@pytest.mark.parametrize("script", [ORCH_INIT, JOIN], ids=["orchestrator-init", "join"])
def test_setup_leaves_a_choir_that_runs(script: Path, tmp_path: Path) -> None:
    """The entry point exists, is executable, and forwards its arguments."""
    proc = _run(script, tmp_path, bin_on_path=True)
    shim = tmp_path / "home" / ".local" / "bin" / "choir"

    assert shim.is_file(), f"no entry point written\n{proc.stdout}\n{proc.stderr}"
    assert shim.stat().st_mode & 0o111, "entry point is not executable"
    # `join.sh` reached the real elan installer here once. Nothing setup
    # would have downloaded may appear in the fixture HOME.
    assert not (tmp_path / "home" / ".elan").exists(), "a tool was installed for real"

    ran = subprocess.run([str(shim), "orch", "prs"], capture_output=True, text=True, check=False)
    assert ran.returncode == 0, ran.stderr
    assert ran.stdout.strip() == f"{STUB_MARKER} orch prs"


@pytest.mark.parametrize("script", [ORCH_INIT, JOIN], ids=["orchestrator-init", "join"])
def test_the_manual_pointer_is_written_to_the_working_folder(
    script: Path, tmp_path: Path
) -> None:
    """Both scripts point the folder they are *run from* at the manual.

    That folder is the run's cwd, not anything derived from CHOIR_DIR, so it
    is the one mutating path the fixture cannot contain by construction —
    this is what holds the writes inside `tmp_path`.
    """
    _run(script, tmp_path, bin_on_path=True)
    work = tmp_path / "work"
    assert (work / "CLAUDE.md").is_file(), "no pointer in the folder the script ran in"
    assert (work / "AGENTS.md").is_file(), "no pointer in the folder the script ran in"


@pytest.mark.parametrize("script", [ORCH_INIT, JOIN], ids=["orchestrator-init", "join"])
def test_path_is_verified_not_assumed(script: Path, tmp_path: Path) -> None:
    """A bin dir the user's PATH does not carry is flagged, not assumed.

    Both scripts prepend `~/.local/bin` to their *own* PATH so tools they
    install mid-run are reachable. Checking `command -v choir` after that
    would always succeed, which is the assumption this pins against.
    """
    proc = _run(script, tmp_path, bin_on_path=False)
    assert "ACTION NEEDED" in proc.stdout, proc.stdout
    assert re.search(r"\.local/bin", proc.stdout), proc.stdout
    assert proc.returncode != 0, "setup reported READY without a reachable choir"


def test_entry_point_installer_is_identical_in_both_scripts() -> None:
    """The duplicated installer must not drift (see module docstring)."""

    def _fn(script: Path) -> str:
        m = re.search(
            r"^install_entry_point\(\) \{$.*?^\}$",
            script.read_text(),
            re.MULTILINE | re.DOTALL,
        )
        assert m, f"install_entry_point() not found in {script.name}"
        return m.group(0)

    assert _fn(ORCH_INIT) == _fn(JOIN)


@pytest.mark.parametrize("script", [ORCH_INIT, JOIN], ids=["orchestrator-init", "join"])
def test_shell_and_update_write_the_same_wrapper(script: Path, tmp_path: Path) -> None:
    """`choir update` refreshes the same file setup wrote.

    Three copies of the wrapper exist — two shell heredocs and
    `client.update.entry_point_script` — so whichever runs last must leave
    the file the others would have. A drift here means `choir update`
    silently rewrites setup's wrapper into something else.
    """
    _run(script, tmp_path, bin_on_path=True)
    written = (tmp_path / "home" / ".local" / "bin" / "choir").read_text()
    assert written == entry_point_script(tmp_path / "choir")
