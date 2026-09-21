"""`choir` — one command, two roles.

Choir's roles are peers (`docs/agents/ORCHESTRATOR.md` § The role): a
worker claims and proves tasks on its own machine and LLM account, an
orchestrator plans, reviews and merges on the overseer's. Neither is the
main one, so neither gets the bare command:

    choir worker claim <repo> <n>
    choir orch --repo <repo> merge <n>

This module is only a router. Each role keeps its own parser, so
`python -m client.cli` and `python -m orchestrator.cli` still work
unchanged.

**Which role you may act in is not decided here.** It is decided by the
GitHub token in play: under spec D4 only the owner has write access and
contributors work from forks, so a worker that runs an `orch` subcommand
gets a 403 from GitHub. Having the command is not having the authority —
see `docs/agents/orchestrator-setup.md` § Permissions.

`update` and `purge` are not roles. One pulls this machine's Choir
checkout to latest, the other clears its local state for a project; both
serve either side, which is why they sit beside the roles rather than
under one. `update` lends the worker parser's subcommand rather than
defining a second one, so `choir worker update` keeps working.
"""

from __future__ import annotations

import sys

from choir_cli import purge as purge_cli
from client import cli as worker_cli
from orchestrator import cli as orch_cli

_USAGE = """choir — community-led formalization behind a deterministic gate.

usage: choir <role> ...

roles:
  worker    claim a task, prove it, submit  (docs/agents/CONTRIBUTOR.md)
  orch      plan, review, merge, replan     (docs/agents/ORCHESTRATOR.md)

maintenance:
  update    pull the Choir checkout, reinstall deps, refresh `choir`
  purge     remove this machine's local state for a project

Run `choir worker --help` or `choir orch --help` for either role's
commands. Which role you may act in is set by your GitHub access, not by
this command.
"""


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    if not args or args[0] in ("-h", "--help"):
        sys.stdout.write(_USAGE)
        return 0

    role, rest = args[0], args[1:]

    # Dispatch to the module, not a bound function: each role keeps its own
    # parser, and the argv after the role name is forwarded verbatim.
    if role == "worker":
        return worker_cli.main(rest)
    if role == "orch":
        return orch_cli.main(rest)
    if role == "update":
        return worker_cli.main(["update", *rest], prog="choir")
    if role == "purge":
        return purge_cli.main(rest)

    print(f"choir: unknown role {role!r}\n", file=sys.stderr)
    sys.stderr.write(_USAGE)
    return 1
