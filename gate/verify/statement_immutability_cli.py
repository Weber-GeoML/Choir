"""CLI: statement-immutability audit on a PR (spec D2).

Reads the PR's changed files via `gate.verify.pr_files.fetch_pr_files`
(paginated — `gh pr view --json files` truncates at 100, so a PR could
otherwise pad itself and push its real change out of the audited set),
fetches base and head versions of each via `git show`, and runs
`statement_immutability.compare_declarations`, aggregating findings
across files. The check name is fixed across provers; which declaration
kinds have a statement/proof-body split comes from the effective
`ProverProfile` (`--prover` flag > base-SHA `.choir/project.toml` >
lean4 default).

Every declaration present at base in every changed file is pinned, not
just one named target — which is why this mirrors `axiom_honesty_cli.py`
rather than `statement_equiv_cli.py`.

Exit codes:
    0 = audit passes (every base declaration reached head untouched, or
        no matching files changed)
    1 = audit fails (a base declaration's statement changed, was
        deleted or renamed, or could not be confirmed unchanged)
    2 = environmental failure (gh/git error, or an unresolvable prover)

`UNDETERMINED` counts as failure here, unlike `statement_equiv` — see
`gate/verify/statement_immutability.py` for why.

Two base-read hazards, both closed and both easy to reintroduce:

- **Renames.** `fetch_pr_files` lists a renamed file under its new path
  only, so `git show base_sha:new_path` finds no blob and every
  declaration carried over reads as newly introduced and passes
  unchecked. `detect_rename` (`gate.verify.base_contents`) resolves the
  pre-rename path for every changed file.
- **Absence vs. failure.** Returning `''` for both "no blob at this SHA"
  (a new file, correctly nothing to pin) and "git show failed" turns an
  infrastructure fault into a fail-open on the base side. `_exists_at`
  (`git cat-file -e`) answers existence before any content is read.
"""

from __future__ import annotations

import argparse
import subprocess
import sys

from gate.provers import ProverError
from gate.verify.base_contents import detect_rename
from gate.verify.pr_files import PrFilesError, fetch_pr_files
from gate.verify.prover_dispatch import filter_files_by_profile, resolve_prover_profile
from gate.verify.statement_immutability import (
    Finding,
    Verdict,
    compare_declarations,
    count_base_declarations,
)


class _ExternalError(RuntimeError):
    pass


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise _ExternalError(
            f"git {' '.join(args)} failed: {(result.stderr or '').strip()}"
        )
    return result.stdout


def _exists_at(sha: str, path: str) -> bool:
    """True iff `path` exists in the tree at `sha`.

    `git cat-file -e` answers "does this path exist in this tree"
    unambiguously (review finding F2) — unlike inferring existence
    from a `git show` failure, which conflates "not there" (the
    legitimate new-file case) with "there, but something else went
    wrong" (an external failure that must not read as a clean pass).
    Not routed through `_git`: a nonzero exit here just means
    "absent," never "external failure."
    """
    result = subprocess.run(
        ["git", "cat-file", "-e", f"{sha}:{path}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def _show(sha: str, path: str) -> str:
    """Return the contents of `path` at `sha`.

    Existence is tested explicitly (`_exists_at`, F2) before any
    content is read. A path that legitimately has no blob at `sha` —
    a file added only in head has no base blob — returns `''`:
    nothing to pin, the correct reading for a genuinely new file. A
    `git show` failure on a path that DOES exist at `sha` is an
    external failure and raises `_ExternalError` (caught in `main`,
    which exits 2) rather than silently collapsing into the same
    `''` a real new file gets — before this fix the two were
    indistinguishable, which failed open on the base side (a broken
    read of a real base file looked like "nothing to check") and
    failed closed for the wrong reason on the head side (a transient
    read failure looked like "the declaration was deleted").
    """
    if not _exists_at(sha, path):
        return ""
    return _git("show", f"{sha}:{path}")


def _base_show(base_sha: str, head_sha: str, path: str) -> str:
    """Base-SHA contents for `path`, following a same-PR rename (F1).

    A file renamed within the PR has no blob at its new path in the
    base tree, so a naive `_show(base_sha, path)` reads it as brand
    new and every declaration it carried over from before the rename
    passes unchecked — the exact bypass this fixes. `detect_rename`
    (`gate.verify.base_contents`, shared with `statement_equiv_cli`)
    resolves the pre-rename name, if any, so the file's real base
    declarations are read from where they actually live. Applied to
    every changed file in the loop below, not just one named target.
    """
    old_path = detect_rename(base_sha, head_sha, path)
    return _show(base_sha, old_path if old_path is not None else path)


def _describe_side(text: str | None, *, absent_label: str) -> str:
    """Render one side of a `Finding` for the human-readable report.

    `None` always means absent (per the `Finding` contract); an empty
    string means present but its statement could not be extracted.
    Keeping these visually distinct matters because the orchestrator
    triages them differently — absence from head is a real edit (either
    a deletion or a rename to a different name, both of which D2
    forbids a worker doing on their own; the check is name-keyed and
    cannot tell the two apart), a parse failure is a "look at this by
    hand."
    """
    if text is None:
        return absent_label
    if text == "":
        return "*(present, but its statement could not be extracted)*"
    return f"`{text}`"


def format_findings(findings: list[Finding]) -> str:
    """Render findings as a Markdown list for status-check output.

    One entry per declaration, showing both sides so the orchestrator
    can see exactly what moved (or that it was deleted, or that this
    module could not tell) without cross-referencing anything else.
    """
    if not findings:
        return "No base declarations were changed."
    lines = []
    for f in findings:
        base_desc = _describe_side(
            f.base_statement, absent_label="*(absent from base)*"
        )
        head_desc = _describe_side(
            f.head_statement,
            absent_label="*(absent from head — deleted, or renamed to a "
            "different declaration name)*",
        )
        lines.append(f"- `{f.decl}`: base = {base_desc}; head = {head_desc}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Statement-immutability audit")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument(
        "--prover",
        default=None,
        help="Override the effective prover. Default: read "
        "[project].prover from .choir/project.toml at the PR's base SHA, "
        "else lean4.",
    )
    args = parser.parse_args(argv)

    try:
        base_sha, head_sha, all_files = fetch_pr_files(args.repo, args.pr)
    except (PrFilesError, _ExternalError) as e:
        # PrFilesError is what the shared fetch actually raises;
        # _ExternalError (this module's own git/gh-failure type, used
        # below by `_show`/`_base_show`) is caught too so this call
        # site keeps mapping any external-command failure to exit 2,
        # however it's signaled.
        print(f"error: {e}", file=sys.stderr)
        return 2

    try:
        profile = resolve_prover_profile(args.prover, base_sha)
    except ProverError as e:
        # F3: an unresolvable prover (unknown --prover, or malformed
        # [project] prover in the base-SHA .choir/project.toml) is an
        # infrastructure failure, not "the contributor changed a
        # statement" — must not fall through to Python's default exit
        # 1, which the exit contract reserves for the latter.
        print(f"error: {e}", file=sys.stderr)
        return 2
    files = filter_files_by_profile(all_files, profile)

    print("=== statement-immutability audit ===")
    print(f"prover: {profile.name}")
    print(f"base: {base_sha}")
    print(f"head: {head_sha}")
    print(f"changed {'/'.join(profile.file_extensions)} files: {len(files)}")
    print()

    if not files:
        print("No matching source files changed; audit not applicable.")
        return 0

    any_failed = False
    for path in files:
        try:
            base = _base_show(base_sha, head_sha, path)
            head = _show(head_sha, path)
        except _ExternalError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        verdict, findings = compare_declarations(base, head, profile=profile)
        if verdict == Verdict.UNCHANGED:
            # "Nothing to check" is not "checked and fine" (round 4, F3).
            # Both are UNCHANGED — correctly, since no base declaration
            # moved either way — but rendering them the same line claims
            # a file was verified when its base version had no
            # enumerable declaration in it at all (a brand-new file, the
            # ordinary case; or an enumeration gap, which is what one
            # looks like from outside). The orchestrator reads these
            # lines to decide where to look, so the distinction is the
            # whole value of the line.
            base_decls = count_base_declarations(base, profile=profile)
            if base_decls == 0:
                print(
                    f"○ {path}: no declarations in the base version; "
                    "nothing to check"
                )
            else:
                print(
                    f"✓ {path}: clean "
                    f"({base_decls} base declaration"
                    f"{'' if base_decls == 1 else 's'} checked)"
                )
            continue

        any_failed = True
        if verdict == Verdict.CHANGED:
            print(f"⚠ {path}: base declaration(s) changed or removed")
        else:
            print(f"⚠ {path}: base declaration(s) could not be confirmed unchanged")
        print(format_findings(findings))
        print()

    if any_failed:
        print()
        print(
            "PR touches the statement of a declaration present in the "
            "base version of a changed file. Per spec D2: the "
            "orchestrator authors every theorem statement, and a "
            "worker's PR is a proof-body fill only. This blocks merge — "
            "including the UNDETERMINED case, since 'could not tell' is "
            "not an honest pass for a check whose job is 'did you touch "
            "anything you weren't supposed to.'"
        )
        return 1
    return 0


# Re-export Finding for callers that want it; keeps the public surface
# of the CLI module discoverable without two imports.
__all__ = ["Finding", "main"]


if __name__ == "__main__":
    sys.exit(main())
