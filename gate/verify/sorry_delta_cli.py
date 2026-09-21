"""CLI: sorry-delta (placeholder-delta) audit on a PR.

Invoked by `.github/workflows/verify-sorry.yml`. Reads the PR's changed
files via the shared `gate.verify.pr_files.fetch_pr_files` (paginated
past `gh pr view --json files`'s 100-file cap), fetches base and head
versions of each prover source file via `git show`, and runs
`sorry_delta.compare`. The check name (`verify-sorry`) stays the
prover-neutral name across all three profiles (design note 12 §2.3);
what counts as a "sorry" — the placeholder tokens and file extensions
scanned — comes from the effective `ProverProfile` (`--prover` flag >
base-SHA `.choir/project.toml` > lean4 default, resolved once via
`gate.verify.prover_dispatch.resolve_prover_profile`).

The sorry policy comes from `.choir/verify.toml` at the **base** SHA
(a PR can't flip its own project to `report` mode):

- `block` (default): net-new placeholders fail the check.
- `report`: net-new placeholders are listed but the check passes —
  blueprint-style projects where partial progress may merge after
  orchestrator review.

The CLI fetches the PR's own body via `gh pr view --json body --jq
.body` (`--repo`/`--pr` are already in hand for `fetch_pr_files`). A
`choir-reduction` block in it holds the submission to
`sorry_delta.compare_reduction` instead of `compare`: a placeholder is
permitted on an obligation the block declares, when that declaration is
new to the PR, and never on the task's own target.

The target comes from the **task**, never from the block.
`gate.state.close_on_merge.parse_closing_refs` reads the linked issue
out of the PR body, `gh issue view` fetches it, and
`gate.state.intake.parse_issue_body` supplies its `target_file` and
`target_decl` — the same resolution `comparator_cli` does in PR mode. A
submitter who could name the declaration their own submission is
checked against would name an unrelated helper and leave the real
target sorried.

Two further conditions bound what a submitter's choice of link can do:

- **The link is bound to the PR at file granularity.** A closing
  reference is body text, so a PR could name a task other than the one
  it works on. The linked task's `target_file` therefore has to be
  among the changed files this audit examines — the pull-request API's
  own list via `fetch_pr_files`, authoritative rather than derived from
  a diff, narrowed to the prover's extensions exactly as the audit loop
  below narrows it, so the contract can never be granted for a file
  that loop never visits. In an honest submission the target file is
  always in it, because the target's proof body changed. `comparator`
  requires the same of the same submission, so the two audits agree on
  whether one is a reduction.
- **The relaxation is scoped to that same `target_file`.** Every other
  changed file is held to the ordinary contract. `docs/agents/CONTRIBUTOR.md`
  already restricts a worker to the target file, so this matches the
  contract the worker is under rather than adding one, and a single
  declared obligation cannot license a placeholder anywhere in the PR.

Together those two bound a forged link to one file: a block cannot
reach a file the linked task does not name. Inside that file the
binding is not complete — a link could name a second task whose target
sits in the same file and list the real target as a mere obligation.
That needs the real target to be **absent from base**, because Rule B
refuses a risen count in a declaration base already had, and the
orchestrator commits the target's placeholder before publishing the
task against it. A target absent from base is the
worker-authored-target blind spot: nothing mechanical catches a statement
the orchestrator never committed, which is stage-two review's job. The
forgery reduces to that; it is not a hole this contract opens.

Every partial reading falls back to the ordinary contract and prints
why: no block, an unusable block, no linked issue, a linked issue whose
body does not parse or carries no `target_file`/`target_decl`, a block
whose `parent` is not the task's target, or a task whose target this PR
does not touch. The relaxation is granted only on a complete, agreeing
reading. A failure to fetch the PR body itself is not among these — see
`_resolve_reduction`.

The exit contract is the policy's either way: under `report` a refused
reduction is listed and the check still passes, exactly as an ordinary
net-new placeholder is, and the orchestrator's review is the
catch-point.

Exit codes:
    0 = audit passes (no net-new placeholders, or policy is `report`)
    1 = audit fails (net-new placeholders under `block`; merge blocked)
    2 = environmental failure (gh/git error, or an unresolvable prover)
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Collection
from dataclasses import dataclass

from gate.provers import ProverError
from gate.state.close_on_merge import parse_closing_refs
from gate.state.intake import ParseSuccess, parse_issue_body
from gate.state.reduction_record import (
    ReductionParseError,
    parse_reduction_block,
)
from gate.verify.config import SorryPolicy, VerifyConfig, load_verify_config_at_sha
from gate.verify.pr_files import PrFilesError, fetch_pr_files
from gate.verify.prover_dispatch import filter_files_by_profile, resolve_prover_profile
from gate.verify.sorry_delta import (
    Verdict,
    compare,
    compare_reduction,
    format_finding,
)

# Re-exported under the audit-conventional name (mirrors
# axiom_honesty_cli); reads from the base SHA by contract.
load_verify_config_from_base = load_verify_config_at_sha


def exit_code_for(*, any_introduced: bool, policy: SorryPolicy) -> int:
    """Map the audit outcome to the process exit code.

    `report` never fails the check: the delta is information for the
    orchestrator's review, not a merge-blocker.
    """
    if any_introduced and policy is SorryPolicy.BLOCK:
        return 1
    return 0


class _ExternalError(RuntimeError):
    pass


def _run(tool: str, *args: str) -> str:
    result = subprocess.run(
        [tool, *args], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise _ExternalError(
            f"{tool} {' '.join(args)} failed: {(result.stderr or '').strip()}"
        )
    return result.stdout


def _show(sha: str, path: str) -> str:
    """Contents of `path` at `sha`, or '' if not present (new file)."""
    try:
        return _run("git", "show", f"{sha}:{path}")
    except _ExternalError:
        return ""


@dataclass(frozen=True)
class _Reduction:
    """The reduction contract one submission is held to.

    `target_file` and `target_decl` are the task's, resolved from the
    linked issue. `children` are the obligations the submission
    declared. The contract applies to `target_file` alone.
    """

    target_file: str
    target_decl: str
    children: tuple[str, ...]


def _task_target(repo: str, pr_body: str) -> tuple[str, str] | str:
    """`(target_file, target_decl)` for the task this PR closes.

    A returned *string* is the reason there is no usable task: the body
    links no issue, the issue's body does not parse, or the record is
    missing either field. Raises `_ExternalError` when `gh` fails, which
    the caller maps to exit 2 rather than to a silent fallback — an
    unread issue is an unknown target, not an absent one.
    """
    closing = parse_closing_refs(pr_body)
    if not closing:
        return "the PR body links no issue with a closing keyword"
    issue = closing[0]
    issue_body = _run(
        "gh", "issue", "view", str(issue), "--repo", repo,
        "--json", "body", "-q", ".body",
    ).strip()
    parsed = parse_issue_body(issue_body, expected_repo=repo)
    if not isinstance(parsed, ParseSuccess):
        return f"issue #{issue}'s body does not parse as a Choir task"
    if not parsed.record.target_file or not parsed.record.target_decl:
        return f"issue #{issue} carries no target_file/target_decl"
    return parsed.record.target_file, parsed.record.target_decl


def _fetch_pr_body(repo: str, pr: int) -> str:
    """The PR's own body, via `gh pr view`.

    Raises `_ExternalError` on a `gh` failure — uncaught here, so it
    propagates through `_resolve_reduction` to `main`'s infrastructure
    exit rather than being read as an absent block. The two are not the
    same fact: a `gh` outage tells us nothing about the submission, so
    it must not be scored as an ordinary one.
    """
    return _run(
        "gh", "pr", "view", str(pr), "--repo", repo,
        "--json", "body", "--jq", ".body",
    )


def _resolve_reduction(
    *,
    repo: str,
    pr: int,
    changed_files: Collection[str],
) -> tuple[_Reduction | None, str | None]:
    """The reduction contract for this submission, if it has one.

    Returns `(reduction, message)`. A `_Reduction` comes back only for a
    present, valid block whose `parent` is the task's `target_decl` and
    whose task names a file this PR changes; every other reading
    returns `None` with a message to print, so the audit falls back to
    the ordinary contract.

    The PR body is submitter-controlled: it is the input that decides
    whether this submission is even read as a reduction, so everything
    derived from it below — the block, the linked issue, the task's
    target — is untrusted and fails closed on any gap or disagreement.
    A `gh` failure fetching it is not one of those gaps; it is raised,
    not folded into `(None, message)`, because an unfetched body is not
    known to lack a block.

    The block's `parent` must quote the task's `target_decl` verbatim.
    Two spellings can denote the same declaration, but nothing here can
    decide that they do, and guessing through a mismatch would grant the
    relaxation on the guess. Quoting it is what the submitter can do
    exactly.

    The linked task's `target_file` must also be one of
    `changed_files` — the files this PR touches that the effective
    prover's profile scans, which is the same list the audit loop
    iterates, so a reduction is never granted on a file that loop skips.
    That binds the link to the submission at file granularity without
    reading a diff: a reduction's target changes only in its proof body,
    which no statement-level detector sees, while the file it lives in
    is changed by construction. The module docstring states what this
    binding does and does not cover.
    """
    body = _fetch_pr_body(repo, pr)

    parsed = parse_reduction_block(body)
    if parsed is None:
        return None, None
    if isinstance(parsed, ReductionParseError):
        return None, f"reduction block ignored — {parsed.message}"

    task = _task_target(repo, body)
    if isinstance(task, str):
        return None, f"reduction block ignored — {task}"
    target_file, target_decl = task
    if parsed.parent != target_decl:
        return None, (
            f"reduction block ignored — its parent {parsed.parent!r} is not "
            f"the task's target {target_decl!r}"
        )

    if target_file not in changed_files:
        return None, (
            f"reduction block ignored — the linked task's target file "
            f"{target_file!r} is not among the files this PR changes, so "
            "the link does not describe this submission"
        )

    return (
        _Reduction(
            target_file=target_file,
            target_decl=target_decl,
            children=tuple(child.decl for child in parsed.children),
        ),
        None,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sorry-delta audit")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument(
        "--prover",
        default=None,
        help="Override the effective prover. Default: read "
        "[project].prover from .choir/project.toml at the PR's base SHA, "
        "else lean4.",
    )
    return parser


def _print_refusal(reduction: _Reduction | None, policy: SorryPolicy) -> None:
    """Explain a run that found placeholders it will not accept.

    Split out of `main` so the three contracts' wordings sit next to
    each other rather than as a third tail branch of the audit loop.
    """
    if reduction is not None:
        print(
            "PR declares itself a reduction. The reduction contract "
            f"applies to the task's target file ({reduction.target_file}) "
            "and no other, since the task scopes a worker to one file; "
            "every other changed file above is held to the ordinary "
            "contract. Under the reduction contract a placeholder is "
            "refused on the task's own target, on a declaration the base "
            "already had, on a declaration name two of that file's "
            "declarations share, or with no enclosing declaration — a "
            "reduction may state the obligations it leans on; it may not "
            "leave its target unproved. A reduction table above lists "
            "only the placeholders that contract disallows, while its "
            "counts are the file's totals, so the delta on it can exceed "
            "the number of rows."
        )
    elif policy is SorryPolicy.BLOCK:
        print(
            "PR increases the number of placeholder proofs (sorry / "
            "oops / Admitted, per prover). The build treats these as "
            "warnings, not errors, so this audit is the merge-blocker: "
            "final submissions must not add unproven obligations. If "
            "this is an intentional progress checkpoint, coordinate "
            "with the project overseer."
        )
    else:
        print(
            "PR increases the number of placeholder proofs. Project "
            "policy is `report`: the check passes, and whether this "
            "partial progress merges is the orchestrator's review "
            "decision. Merged placeholders enter the trust inventory "
            "as open obligations."
        )


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    try:
        base_sha, head_sha, all_files = fetch_pr_files(args.repo, args.pr)
    except (PrFilesError, _ExternalError) as e:
        # PrFilesError is what the shared fetch actually raises;
        # _ExternalError (this module's own git/gh-failure type, used
        # below by `_show`) is caught too so this call site keeps
        # mapping any external-command failure to exit 2, however it's
        # signaled.
        print(f"error: {e}", file=sys.stderr)
        return 2

    try:
        profile = resolve_prover_profile(args.prover, base_sha)
    except ProverError as e:
        # An unresolvable prover (unknown --prover, or malformed
        # [project] prover in the base-SHA .choir/project.toml) is an
        # infrastructure failure, not "the contributor introduced a
        # placeholder" — must not fall through to Python's default
        # exit 1, which the exit contract reserves for the latter.
        print(f"error: {e}", file=sys.stderr)
        return 2
    files = filter_files_by_profile(all_files, profile)

    config: VerifyConfig = load_verify_config_from_base(base_sha)
    policy = config.sorry_delta.policy

    try:
        reduction, reduction_message = _resolve_reduction(
            repo=args.repo, pr=args.pr, changed_files=files
        )
    except _ExternalError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    print("=== sorry-delta audit ===")
    print(f"prover: {profile.name}")
    print(f"base: {base_sha}")
    print(f"head: {head_sha}")
    print(f"policy: {policy.value}")
    print(f"submission: {'reduction' if reduction is not None else 'proof'}")
    if reduction is not None:
        print(f"reduction target: {reduction.target_decl} in {reduction.target_file}")
    if reduction_message is not None:
        print(f"⚠ {reduction_message}")
    print(f"changed {'/'.join(profile.file_extensions)} files: {len(files)}")
    print()

    if not files:
        print("No matching source files changed; audit not applicable.")
        return 0

    any_introduced = False
    for path in files:
        base = _show(base_sha, path)
        head = _show(head_sha, path)
        reduced = reduction is not None and path == reduction.target_file
        if reduced:
            assert reduction is not None
            verdict, finding = compare_reduction(
                base,
                head,
                target_decl=reduction.target_decl,
                children=reduction.children,
                file_path=path,
                profile=profile,
            )
        else:
            verdict, finding = compare(base, head, file_path=path, profile=profile)
        if verdict == Verdict.INTRODUCED:
            any_introduced = True
            print(
                f"⚠ {path}: placeholders the reduction contract disallows"
                if reduced
                else f"⚠ {path}: net-new placeholders"
            )
            assert finding is not None
            print(format_finding(finding))
            print()
        else:
            print(f"✓ {path}: clean")

    if any_introduced:
        print()
        _print_refusal(reduction, policy)

    return exit_code_for(any_introduced=any_introduced, policy=policy)


if __name__ == "__main__":
    sys.exit(main())
