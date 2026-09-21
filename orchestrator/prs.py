"""PR primitives for the orchestrator agent.

The review/merge half of the orchestrator loop: list open PRs with
their deterministic-audit status, read the diff, post review feedback,
and merge or close — all through the overseer's local `gh` auth.

The merge decision itself is the agent's job (and is governed by the
project's automation level — see `orchestrator.project_config`). These
functions never decide anything; they read and execute.

Choir's trust floor does not rest on branch protection here. Protection is
unavailable on private repos without a paid plan, and once spec D4's
`enforce_admins: false` bootstrap lands the owner — whose auth the
orchestrator uses — is exempt by design. `merge_pr` therefore enforces the
gate itself: it refuses any PR whose blocking checks are not green, or whose
required checks never ran.

What that enforcement does *not* cover: it verifies the checks that ran, by
name and conclusion, so it cannot detect a PR that changed *which* checks
run — a stub job carrying a required name, or an existing workflow whose
`run:` step was gutted, both look green. Closing that needs a protected-paths
gate audit (the 2026-05-07 decision, still unimplemented). Until then the
orchestrator refuses any PR touching `.github/` or `.choir/` outright — see
`docs/agents/ORCHESTRATOR.md` § Boundaries.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass, replace

from gate.checks import is_blocking, missing_required
from gate.state.close_on_merge import parse_closing_refs


class PRError(RuntimeError):
    """A `gh` operation failed in a PR primitive."""


@dataclass(frozen=True)
class CheckResult:
    """One status check on a PR head."""

    name: str
    status: str       # e.g. "COMPLETED", "IN_PROGRESS", "QUEUED"
    conclusion: str   # e.g. "SUCCESS", "FAILURE", "" while running


@dataclass(frozen=True)
class PRView:
    """One pull request, with everything the orchestrator needs to act."""

    number: int
    title: str
    url: str
    author: str
    state: str               # "open" / "closed" / "merged"
    body: str
    base_sha: str
    head_sha: str
    labels: tuple[str, ...]
    files: tuple[str, ...]   # changed file paths
    checks: tuple[CheckResult, ...]
    linked_issues: tuple[int, ...]  # from closes/fixes/resolves keywords
    mergeable: str           # "MERGEABLE" / "CONFLICTING" / "UNKNOWN"
    # "owner/name" of the head — a contributor's fork for every ordinary
    # contribution since spec D4. Defaulted so existing construction sites
    # keep working; empty means "gh didn't report it", which callers must
    # treat as "may be a fork" rather than as same-repo.
    head_repo: str = ""
    files_truncated: bool = False

    @property
    def checks_pending(self) -> bool:
        return any(c.status != "COMPLETED" for c in self.checks)

    @property
    def checks_all_green(self) -> bool:
        """True iff every check completed successfully.

        False while anything is still running — callers that want to
        distinguish "failed" from "not finished" check `checks_pending`.
        Vacuously False when there are no checks at all: a repo without
        the gate workflows shouldn't look green.
        """
        if not self.checks:
            return False
        return all(
            c.status == "COMPLETED" and c.conclusion == "SUCCESS"
            for c in self.checks
        )

    def as_dict(self) -> dict[str, object]:
        d = asdict(self)
        for key in ("labels", "files", "linked_issues"):
            d[key] = list(d[key])
        d["checks"] = [asdict(c) for c in self.checks]
        d["checks_all_green"] = self.checks_all_green
        d["checks_pending"] = self.checks_pending
        return d


def _gh(*args: str) -> str:
    try:
        result = subprocess.run(
            ["gh", *args], capture_output=True, text=True, check=True
        )
        return result.stdout
    except FileNotFoundError as e:
        raise PRError(
            "`gh` CLI not found in PATH — install it from https://cli.github.com"
        ) from e
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or "").strip()
        raise PRError(f"gh {' '.join(args)} failed — {stderr}") from e


_PR_JSON_FIELDS = (
    "number,title,url,author,state,body,baseRefOid,headRefOid,"
    "labels,files,statusCheckRollup,mergeable,headRepository,headRepositoryOwner"
)

# `gh pr view --json files` truncates at 100 changed files with no error and
# no marker — the same cap five gate CLIs were fixed to paginate past. It
# still matters here: `docs/agents/ORCHESTRATOR.md` § Boundaries has the
# orchestrator refuse any PR touching `.github/`, and that rule reads
# `pr.files`. A PR padded past the cap could push its `.github/` edit out of
# the list the refusal ever sees. `get_pr` therefore paginates; the
# survey-shaped `list_open_prs` keeps the cheap one-request view and flags
# `files_truncated` so a caller cannot mistake a truncated list for a
# complete one.
_FILES_PAGE_CAP = 100


# Terminal commit-status states of a legacy `StatusContext`. GitHub's
# commit-status API has no `status`/`conclusion` split — one `state` carries
# both — so these map onto COMPLETED + the matching conclusion. Anything
# else (notably `pending`) is deliberately absent: a non-terminal state must
# keep reading as not-COMPLETED so it still blocks a merge.
_TERMINAL_STATUS_STATES = frozenset({"SUCCESS", "FAILURE", "ERROR"})


def _parse_pr(it: dict[str, object]) -> PRView:
    checks: list[CheckResult] = []
    for c in it.get("statusCheckRollup") or []:  # type: ignore[union-attr]
        # gh mixes CheckRun and StatusContext shapes in the rollup;
        # normalize to name/status/conclusion with safe fallbacks.
        status = str(c.get("status") or "")
        conclusion = str(c.get("conclusion") or "")
        if not status:
            # StatusContext shape: no `status` field at all, one `state`
            # holding a terminal commit-status value. Left un-normalized, a
            # green third-party status (Codecov, DCO) read as
            # blocking-and-never-COMPLETED and wedged every merge
            # permanently, with `force` as the only escape.
            state = str(c.get("state") or "").upper()
            if state in _TERMINAL_STATUS_STATES:
                status, conclusion = "COMPLETED", state
            else:
                status = state
        checks.append(
            CheckResult(
                name=str(c.get("name") or c.get("context") or "unknown"),
                status=status,
                conclusion=conclusion,
            )
        )
    body = str(it.get("body") or "")
    author = it.get("author") or {}
    return PRView(
        number=int(it["number"]),  # type: ignore[arg-type]
        title=str(it["title"]),
        url=str(it["url"]),
        author=str(author.get("login", "")),  # type: ignore[union-attr]
        state=str(it["state"]).lower(),
        body=body,
        base_sha=str(it.get("baseRefOid") or ""),
        head_sha=str(it.get("headRefOid") or ""),
        labels=tuple(
            lbl["name"] for lbl in it.get("labels") or []  # type: ignore[union-attr,index]
        ),
        files=tuple(
            f["path"] for f in it.get("files") or []  # type: ignore[union-attr,index]
        ),
        checks=tuple(checks),
        linked_issues=tuple(parse_closing_refs(body)),
        mergeable=str(it.get("mergeable") or "UNKNOWN"),
        head_repo=_head_repo(it),
        files_truncated=len(it.get("files") or []) >= _FILES_PAGE_CAP,  # type: ignore[arg-type]
    )


def _head_repo(it: dict[str, object]) -> str:
    """`"owner/name"` of the PR's head repository, or `""` if gh didn't say.

    gh reports the two halves separately (`headRepository` is the bare name,
    `headRepositoryOwner` the login), and either can be null on a PR whose
    fork has since been deleted.
    """
    name = (it.get("headRepository") or {}).get("name") or ""  # type: ignore[union-attr]
    owner = (it.get("headRepositoryOwner") or {}).get("login") or ""  # type: ignore[union-attr]
    return f"{owner}/{name}" if owner and name else ""


def list_open_prs(repo: str, *, limit: int = 100) -> list[PRView]:
    """All open PRs in `repo`, newest first (gh default ordering)."""
    out = _gh(
        "pr", "list",
        "--repo", repo,
        "--state", "open",
        "--json", _PR_JSON_FIELDS,
        "--limit", str(limit),
    )
    return [_parse_pr(it) for it in json.loads(out)]


def get_pr(repo: str, number: int) -> PRView:
    """One PR by number (any state), with a complete file list.

    The single-PR read is the one the merge preflight and the stage-two
    review use, so its `files` must be complete: the protected-path refusal
    in `docs/agents/ORCHESTRATOR.md` § Boundaries reads it, and a PR padded past
    `gh pr view --json files`'s silent 100-file cap could otherwise push its
    `.github/` edit out of the list that refusal ever examines. Paginating
    costs one extra request on a PR of any size, which is the right trade
    here and the wrong one in `list_open_prs`.
    """
    out = _gh(
        "pr", "view", str(number),
        "--repo", repo,
        "--json", _PR_JSON_FIELDS,
    )
    it = json.loads(out)
    pr = _parse_pr(it)
    if not pr.files_truncated:
        return pr
    return replace(pr, files=tuple(_all_changed_files(repo, number)),
                   files_truncated=False)


def _all_changed_files(repo: str, number: int) -> list[str]:
    """Every changed path on the PR, paginated past the 100-file cap."""
    out = _gh(
        "api", "--paginate",
        f"repos/{repo}/pulls/{number}/files",
        "-X", "GET", "-f", "per_page=100",
        "--jq", ".[].filename",
    )
    return [line for line in out.splitlines() if line.strip()]


def get_pr_diff(repo: str, number: int) -> str:
    """The PR's unified diff, as text."""
    return _gh("pr", "diff", str(number), "--repo", repo)


def post_comment(repo: str, number: int, body: str) -> None:
    """Post a conversation comment on an issue or a PR.

    A PR *is* an issue to the API, and this is the endpoint `get_pr_comments`
    already reads, so one number is enough — the orchestrator answers a task
    and a submission the same way.
    """
    _gh(
        "api", f"repos/{repo}/issues/{number}/comments",
        "-f", f"body={body}", "--silent",
    )


def blocking_failures(pr: PRView, *, prover: str | None = None) -> list[str]:
    """Blocking checks on `pr` that ran but have not completed successfully.

    Pending (including `QUEUED`) counts as a failure for merge purposes: a
    check still running or waiting to run has not vouched for anything.
    Advisory checks are ignored entirely — see `gate.checks`. A required
    check that never ran at all is a different situation, not covered here
    — see `missing_required_checks`.

    `prover` is forwarded to `is_blocking` unchanged (never defaulted to a
    specific prover here) — an omitted prover must get the strict answer,
    same as `is_blocking` itself. The caller resolves the project's prover
    once per loop and passes it in; this function does not read
    `.choir/project.toml` itself, keeping this module a thin `gh` wrapper.
    """
    return [
        c.name
        for c in pr.checks
        if is_blocking(c.name, prover=prover)
        and not (c.status == "COMPLETED" and c.conclusion == "SUCCESS")
    ]


def pending_blocking_checks(pr: PRView, *, prover: str | None = None) -> list[str]:
    """Blocking checks on `pr` that have not finished running.

    The half of `blocking_failures` that is "not yet" rather than "no": a
    caller deciding whether to come back later wants this, a caller deciding
    whether to merge wants `blocking_failures` (which counts pending as a
    failure, because a check still running has vouched for nothing).

    Advisory checks are excluded deliberately: a check that never blocks a
    merge should not hold a green PR pending either, or a slow-but-advisory
    workflow could keep review waiting on a verdict nothing acts on. (Note
    14 §8 promoted `comparator` to blocking; its own `timeout-minutes: 60`
    is exactly the cost of the audit now being load-bearing, not something
    this function excuses it from.)

    `prover` is forwarded to `is_blocking` unchanged, same rule as
    `blocking_failures` above: an omitted prover gets the strict answer.
    """
    return [
        c.name
        for c in pr.checks
        if is_blocking(c.name, prover=prover) and c.status != "COMPLETED"
    ]


def missing_required_checks(pr: PRView) -> list[str]:
    """Required checks (`gate.checks.REQUIRED_PRESENT`) absent from `pr` entirely.

    Distinct from `blocking_failures`: a check that ran and failed means the
    code is unsound or evasive; a check that never ran at all — e.g. because
    its workflow file was deleted on the PR's own head branch, so the
    workflow simply never triggers for that PR — means no audit happened at
    all. Both must refuse a merge, but they're different diagnoses, so the
    preflight names them apart rather than folding one into the other.
    """
    return missing_required(c.name for c in pr.checks)


def merge_pr(
    repo: str,
    number: int,
    *,
    method: str = "squash",
    delete_branch: bool = True,
    force: str | None = None,
    prover: str | None = None,
) -> None:
    """Merge the PR, refusing unless every required check is present and green.

    `prover` is forwarded to `blocking_failures` unchanged (never defaulted
    to a specific prover) — the caller resolves the project's prover once
    per loop and passes it in, so an omitted prover gets the strict
    (base `gate.checks.CHECKS`) answer rather than silently un-gating a
    prover this function has never heard of.

    The preflight is not redundant with branch protection. Protection is
    unavailable on free private repos, and once spec D4's
    `enforce_admins: false` bootstrap lands the orchestrator runs on the
    owner's auth and the owner is exempt, so GitHub does not constrain this
    call at any automation level — this check is the enforcement. Its bound:
    it judges the checks that ran, so it cannot see a PR that changed which
    checks run (module docstring).

    The PR view is fetched here rather than accepted from the caller: a stale
    view is a bypass. A required check that never ran at all (its workflow
    was deleted on the PR's own branch) is refused exactly like a failing
    one — see `missing_required_checks`. The merge itself is pinned to the
    exact head commit the checks were just read against
    (`--match-head-commit`), so a push landing between the read and the
    merge cannot ride through underneath a check that was genuinely green a
    moment ago.

    **`force` is overseer-authorized.** It exists for the human who owns the
    project, not for the orchestrator's own judgment: an autonomous agent
    that overrides its only enforcement when a check looks inconvenient has
    no enforcement. A check that stays red for reasons outside the diff is an
    escalation, and the orchestrator keeps working everything else meanwhile
    (`docs/agents/ORCHESTRATOR.md` § Automation levels, § Boundaries).

    `force` takes a non-blank reason string, never a bare truthy value — the
    obvious wrong idiom after seeing this function refuse is `force=True`,
    which would merge silently with no attributable reason, so that and any
    non-string or blank value are rejected outright. The reason, plus
    whatever it overrode, is posted to the PR before merging, so an override
    is never silent and remains auditable after the fact — and the override
    path is pinned to `pr.head_sha` too, so the commit that merges is the one
    the trail names. The posted state names the prover the override was
    resolved against: `blocking_failures`/`missing_required_checks` only
    surface checks that are actually blocking *for that prover*, so a check
    that is visibly red but relaxed to advisory there (`statement-equiv` on
    lean4) would otherwise vanish from the trail entirely — "all required
    checks were green" is true of the resolved answer, not of every check's
    literal conclusion, and a human auditing the override wants to see that
    distinction, not have it silently folded away. `method` is one of
    "squash" / "merge" / "rebase".
    """
    if method not in {"squash", "merge", "rebase"}:
        raise PRError(f"invalid merge method: {method!r}")

    if force is not None and (not isinstance(force, str) or not force.strip()):
        raise PRError(
            f"force requires a non-blank reason string, not {force!r} — "
            "a merge override must be attributable."
        )

    pr = get_pr(repo, number)

    if force is None:
        if not pr.checks:
            raise PRError(
                f"PR #{number} has no checks — refusing to merge. A repo "
                "without the gate workflows must not look mergeable."
            )
        missing = missing_required_checks(pr)
        if missing:
            raise PRError(
                f"PR #{number} is missing required checks entirely: "
                f"{', '.join(missing)} — refusing to merge. A check that "
                "never ran (e.g. its workflow was removed on this branch) "
                "is not evidence of passing. If the PR touches `.github/`, "
                "reject it; otherwise escalate to the overseer."
            )
        failures = blocking_failures(pr, prover=prover)
        if failures:
            raise PRError(
                f"PR #{number} has blocking checks not green: "
                f"{', '.join(sorted(failures))} — refusing to merge. Read the "
                "failing check and either give the contributor actionable "
                "feedback or reject the PR. A check that stays red for "
                "reasons outside the diff (infrastructure, a broken runner) "
                "is an escalation to the overseer, not something to work "
                "around: only an overseer authorizes an override."
            )
    else:
        missing = missing_required_checks(pr)
        failures = blocking_failures(pr, prover=prover)
        # Checks that are visibly red (or never completed) but did NOT make
        # `failures` — because they're genuinely advisory, or because this
        # prover's `PROVER_OVERRIDES` relaxed them — are invisible to the
        # override trail otherwise. A human auditing the override wants to
        # know a check was red even though it wasn't blocking, so name it.
        non_blocking_red = sorted(
            c.name
            for c in pr.checks
            if not (c.status == "COMPLETED" and c.conclusion == "SUCCESS")
            and c.name not in failures
        )
        overridden: list[str] = []
        if missing:
            overridden.append(f"missing entirely: {', '.join(missing)}")
        if failures:
            overridden.append(f"failing: {', '.join(sorted(failures))}")
        basis = f"prover={prover!r}" if prover is not None else "no prover given (strict)"
        state = (
            "; ".join(overridden)
            if overridden
            else f"all required checks were green (resolved for {basis})"
        )
        if non_blocking_red:
            state += (
                f"; non-blocking red under that resolution: "
                f"{', '.join(non_blocking_red)}"
            )
        post_comment(
            repo,
            number,
            f"**Merge preflight overridden.** Reason: {force}\n"
            f"Overridden state at {pr.head_sha}: {state}.",
        )

    args = ["pr", "merge", str(number), "--repo", repo, f"--{method}"]
    # Under spec D4 the head branch lives on the contributor's fork, which
    # the orchestrator has no write access to. Asking gh to delete it there
    # can fail *after* the merge has already landed, turning a successful
    # merge into a raised PRError — the worst shape of failure, since the
    # obvious response (re-run the merge) then hits an already-merged PR.
    # An unknown head repo (`""`, e.g. the fork was deleted) is treated as
    # cross-repo for the same reason: skipping a cleanup is free, failing a
    # completed merge is not.
    if delete_branch and pr.head_repo == repo:
        args.append("--delete-branch")
    # Pinned on both paths. On the force path the audit trail *is* the whole
    # justification, and the override comment names a specific head SHA — if
    # a push landed in between, an unpinned merge would land a different
    # commit than the one the trail claims was overridden.
    args.extend(["--match-head-commit", pr.head_sha])
    _gh(*args)


def close_pr(repo: str, number: int, *, comment: str | None = None) -> None:
    """Close the PR without merging, optionally leaving a final comment."""
    args = ["pr", "close", str(number), "--repo", repo]
    if comment:
        args.extend(["--comment", comment])
    _gh(*args)


def get_pr_comments(repo: str, number: int) -> list[tuple[str, str]]:
    """(author_login, body) for each conversation comment on a PR.

    PR conversation comments are issue comments in the GitHub API.
    Review-thread (diff) comments are deliberately not included.
    """
    out = _gh("api", f"repos/{repo}/issues/{number}/comments")
    items = json.loads(out)
    return [(it["user"]["login"], it.get("body") or "") for it in items]
