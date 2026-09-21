"""CLI for `issue-close-on-merge` workflow.

Reads a PR body from `--body-file`, prints one issue number per line
that should be marked `choir/done` and closed. Empty output is valid
(no closing references; the workflow exits without acting on any issue).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from gate.state.close_on_merge import parse_closing_refs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extract closing-issue refs from a PR body")
    parser.add_argument("--body-file", required=True, help="Path to a file containing the PR body")
    args = parser.parse_args(argv)

    body = Path(args.body_file).read_text(encoding="utf-8")
    for n in parse_closing_refs(body):
        print(n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
