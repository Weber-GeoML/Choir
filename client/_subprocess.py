"""Shared subprocess helpers with consistent error handling.

All `gh` / `git` invocations across `client/` should go through these
helpers so missing binaries produce a uniform, friendly error rather
than a raw `FileNotFoundError` traceback. Caller-domain exception
types (`GitHubError`, `SubmitError`, `WorkspaceError`, ...) wrap a
`ToolNotFound` (subclass of `RuntimeError`) so each module can map to
its own error vocabulary while still surfacing the same root cause.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


class ToolNotFound(RuntimeError):
    """A required CLI binary (gh, git, lake, etc.) wasn't found in PATH."""

    def __init__(self, tool: str, install_hint: str = "") -> None:
        msg = f"required CLI not found in PATH: {tool}"
        if install_hint:
            msg = f"{msg} — {install_hint}"
        super().__init__(msg)
        self.tool = tool


@dataclass(frozen=True)
class CompletedRun:
    """Result of a successful subprocess invocation."""

    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


_INSTALL_HINTS = {
    "gh": "install from https://cli.github.com",
    "git": "install via your system package manager",
    "lake": "install Lean toolchain via https://github.com/leanprover/elan",
    "lean": "install Lean toolchain via https://github.com/leanprover/elan",
    "uv": "install from https://docs.astral.sh/uv/",
}


def run(
    cmd: Sequence[str],
    *,
    cwd: Path | None = None,
    input_: str | None = None,
    timeout: float | None = None,
) -> CompletedRun:
    """Run `cmd` and return a `CompletedRun`. Never raises on non-zero exit.

    Raises `ToolNotFound` if the binary at `cmd[0]` is missing.
    """
    tool = cmd[0]
    try:
        result = subprocess.run(
            list(cmd),
            cwd=cwd,
            input=input_,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except FileNotFoundError as e:
        raise ToolNotFound(tool, _INSTALL_HINTS.get(tool, "")) from e
    return CompletedRun(
        returncode=result.returncode,
        stdout=result.stdout or "",
        stderr=result.stderr or "",
    )
