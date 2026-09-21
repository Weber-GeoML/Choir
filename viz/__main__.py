"""CLI: build a proof-tree timeline for a project.

Standing in a project folder, nothing needs naming — git supplies the
checkout and the remote supplies the slug:

    python -m viz report

which is what `choir orch viz` runs. The explicit form names its inputs,
and splitting the fetch out means a second render costs no second fetch:

    python -m viz fetch <owner/repo> -o payload.json
    python -m viz build <owner/repo> --checkout <path> --payload payload.json

Reads only. Nothing here writes to the project repo or to GitHub; the one
file it writes is the page.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from viz.github import FetchError, Payload, fetch
from viz.gitscan import GitScanError
from viz.report import ReportError, build_report, discover, report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m viz")
    sub = parser.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="record a project's GitHub history")
    f.add_argument("repo")
    f.add_argument("-o", "--out", default="payload.json")

    r = sub.add_parser(
        "report", help="render this folder's project, discovering everything"
    )
    r.add_argument(
        "--scope", choices=("plan", "all"), default="plan",
        help="plan: only declarations the roadmap names (default)."
    )

    b = sub.add_parser("build", help="derive and render a timeline")
    b.add_argument("repo")
    b.add_argument(
        "--checkout", required=True, help="local clone of the project repo"
    )
    b.add_argument("--payload", help="reuse a recorded fetch instead of calling gh")
    b.add_argument("--branch", default="HEAD")
    b.add_argument(
        "--scope",
        choices=("plan", "all"),
        default="plan",
        help="plan: only declarations the plan names (default). "
        "all: every declaration in the corpus, most of them local helpers.",
    )
    b.add_argument("-o", "--out", default="tree.html")

    args = parser.parse_args(argv)

    try:
        if args.cmd == "fetch":
            payload = fetch(args.repo)
            Path(args.out).write_text(
                json.dumps(payload.as_dict(), indent=1), encoding="utf-8"
            )
            print(
                f"{args.out}: {len(payload.issues)} issues, {len(payload.prs)} PRs"
            )
            return 0

        if args.cmd == "report":
            drawn = report(scope=args.scope)
            print(json.dumps(drawn, indent=1))
            return 0

        checkout = Path(args.checkout).expanduser()
        if not (checkout / ".git").exists():
            print(f"error: {checkout} is not a git checkout", file=sys.stderr)
            return 2

        found = discover(checkout, repo=args.repo)
        drawn = build_report(
            repo=found.repo,
            checkout=found.checkout,
            prover=found.prover,
            out=Path(args.out),
            payload=(
                Payload.from_dict(json.loads(Path(args.payload).read_text()))
                if args.payload
                else None
            ),
            branch=args.branch,
            scope=args.scope,
        )
        print(
            f"{drawn['out']}: {drawn['nodes']} nodes, {drawn['edges']} edges, "
            f"{drawn['events']} events in scope {drawn['scope']} "
            f"(of {drawn['declarations_in_history']} declarations in history)"
        )
        for note in drawn["notes"]:
            print(f"  note: {note}")
        return 0

    except (FetchError, GitScanError, ReportError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
