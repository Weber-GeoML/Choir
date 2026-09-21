"""CLI for Choir per-task metrics.

Subcommands:
    summarize <repo>   one row per Choir-labeled issue (lifecycle metrics)
    struggle  <repo>   per-task attempt history — the stuck-task signal the
                       orchestrator reads each loop (docs/agents/orchestrator-planning.md)

Both emit JSON (default) or CSV, designed to pipe into the overseer's own
tooling (jq, awk, pandas, a dashboard). Choir ships no downstream renderers.

Exit codes:
    0 — collection succeeded (output on stdout)
    1 — invalid arguments
    2 — gh / network failure
"""

from __future__ import annotations

import argparse
import csv
import json
import sys

from orchestrator.metrics.collect import (
    MetricsError,
    TaskMetric,
    collect_task_metrics,
)
from orchestrator.metrics.struggle import (
    StruggleSignal,
    collect_struggle_signals,
)


def _emit_json(metrics: list[TaskMetric], stream) -> None:  # type: ignore[no-untyped-def]
    json.dump([m.as_dict() for m in metrics], stream, indent=2, sort_keys=False)
    stream.write("\n")


_CSV_FIELDS = [
    "number",
    "title",
    "url",
    "state",
    "task_type",
    "created_at",
    "closed_at",
    "duration_seconds",
    "contributor",
    "labels",
]


def _emit_csv(metrics: list[TaskMetric], stream) -> None:  # type: ignore[no-untyped-def]
    writer = csv.writer(stream)
    writer.writerow(_CSV_FIELDS)
    for m in metrics:
        writer.writerow(
            [
                m.number,
                m.title,
                m.url,
                m.state,
                m.task_type or "",
                m.created_at,
                m.closed_at or "",
                m.duration_seconds if m.duration_seconds is not None else "",
                m.contributor or "",
                ";".join(m.labels),
            ]
        )


_STRUGGLE_CSV_FIELDS = [
    "number",
    "title",
    "url",
    "state",
    "age_days",
    "attempts",
    "merged",
    "failed",
    "open",
    "distinct_attempters",
    "last_attempt_age_days",
]


def _emit_struggle_json(signals: list[StruggleSignal], stream) -> None:  # type: ignore[no-untyped-def]
    json.dump([s.as_dict() for s in signals], stream, indent=2, sort_keys=False)
    stream.write("\n")


def _emit_struggle_csv(signals: list[StruggleSignal], stream) -> None:  # type: ignore[no-untyped-def]
    writer = csv.writer(stream)
    writer.writerow(_STRUGGLE_CSV_FIELDS)
    for s in signals:
        writer.writerow(
            [
                s.number,
                s.title,
                s.url,
                s.state,
                s.age_days if s.age_days is not None else "",
                s.attempts,
                s.merged,
                s.failed,
                s.open,
                s.distinct_attempters,
                s.last_attempt_age_days if s.last_attempt_age_days is not None else "",
            ]
        )


def _run_summarize(args: argparse.Namespace) -> int:
    try:
        metrics = collect_task_metrics(
            args.repo,
            state=args.state,
            label=args.label,
            limit=args.limit,
        )
    except MetricsError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if args.format == "csv":
        _emit_csv(metrics, sys.stdout)
    else:
        _emit_json(metrics, sys.stdout)
    return 0


def _run_struggle(args: argparse.Namespace) -> int:
    try:
        signals = collect_struggle_signals(
            args.repo,
            state=args.state,
            label=args.label,
            limit=args.limit,
        )
    except MetricsError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if args.min_failed > 0:
        signals = [s for s in signals if s.failed >= args.min_failed]

    if args.format == "csv":
        _emit_struggle_csv(signals, sys.stdout)
    else:
        _emit_struggle_json(signals, sys.stdout)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="orchestrator.metrics",
        description="Emit per-task metrics for a Choir project repo.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("summarize", help="emit one row per Choir issue")
    s.add_argument("repo", help="GitHub repo, e.g. alice/proj")
    s.add_argument(
        "--state",
        choices=["open", "closed", "all"],
        default="all",
        help="filter by issue state (default: all)",
    )
    s.add_argument(
        "--label",
        default=None,
        help="optional additional label filter (e.g. benchmark/quals2026)",
    )
    s.add_argument(
        "--limit",
        type=int,
        default=1000,
        help="max issues to fetch (default 1000)",
    )
    s.add_argument(
        "--format",
        choices=["json", "csv"],
        default="json",
        help="output format (default: json)",
    )

    g = sub.add_parser(
        "struggle",
        help="per-task attempt history (the stuck-task signal)",
    )
    g.add_argument("repo", help="GitHub repo, e.g. alice/proj")
    g.add_argument(
        "--state",
        choices=["open", "closed", "all"],
        default="open",
        help="filter by issue state (default: open — stuck tasks are open)",
    )
    g.add_argument(
        "--label",
        default=None,
        help="optional additional label filter (e.g. benchmark/quals2026)",
    )
    g.add_argument(
        "--min-failed",
        type=int,
        default=0,
        help="only show tasks with >= this many failed (closed-unmerged) attempts",
    )
    g.add_argument(
        "--limit",
        type=int,
        default=1000,
        help="max issues/PRs to fetch (default 1000)",
    )
    g.add_argument(
        "--format",
        choices=["json", "csv"],
        default="json",
        help="output format (default: json)",
    )

    args = parser.parse_args(argv)

    if args.cmd == "summarize":
        return _run_summarize(args)
    if args.cmd == "struggle":
        return _run_struggle(args)
    parser.error(f"unknown subcommand: {args.cmd}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
