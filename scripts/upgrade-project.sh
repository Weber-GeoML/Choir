#!/usr/bin/env bash
# upgrade-project.sh — refresh a bootstrapped Choir project's gate
# overlay to the running Choir checkout's current version (design note
# 13 §4). Deliberate, atomic (one commit), additive, idempotent.
#
# Usage (run from the Choir checkout root):
#   scripts/upgrade-project.sh <path-to-project-clone> [--repo <owner/name>]
#                              [--no-push] [--force-build-workflow]
#
#   <path-to-project-clone>   a local clone of the project repo (the
#                             orchestrator's checkout), on its default
#                             branch, with a clean working tree.
#   --repo <owner/name>       enables the two network-touching steps:
#                             re-running setup-labels.sh, and checking
#                             the clone is on the repo's actual GitHub
#                             default branch. Without it, the script
#                             stays fully offline (accepts whatever
#                             branch is checked out, skips labels).
#   --no-push                 commit locally but don't push.
#   --force-build-workflow    also overwrite verify-pr.yml (see below).
#
# What gets synced (the SAME file set new-project.sh writes, refreshed
# to the running Choir's versions):
#   - gate/ orchestrator/ client/         (rsync -a --delete, no __pycache__)
#   - pyproject.toml, uv.lock
#   - .github/ISSUE_TEMPLATE/
#   - the Python-only verify workflows + issue-intake/close-on-merge/reconcile
#     (these are project-agnostic — no per-project templating — so they are
#     always refreshed by direct copy from THIS checkout's templates/workflows/)
#   - verify-trust-report.yml, prover=lean4 only
#
# verify-pr.yml is deliberately NOT in that copy set: it is prover-
# templated at bootstrap time (a heredoc inside new-project.sh, not a
# static file — Choir's OWN .github/workflows/verify-pr.yml builds
# samples/lean4/, which is Choir-repo-specific and not what a
# generated project needs) and is commonly overseer-adapted afterward.
# By default this script leaves it alone and prints a notice.
# --force-build-workflow regenerates it from the same per-prover
# template new-project.sh uses (duplicated here deliberately, not
# sourced from a shared file, to avoid coupling the two scripts'
# control flow — keep the two heredocs in sync by hand if either
# changes). verify-trust-report.yml is similarly regenerated from that
# template (not copied from this repo's own copy, which also assumes
# the samples/lean4/ layout) whenever the prover is lean4.
#
# Pin: writes [project] choir_protocol / choir_commit in
# .choir/project.toml (gate.upgrade.pin) to this checkout's
# PROTOCOL_VERSION + HEAD commit.
#
# Idempotent: if nothing in the overlay set + .choir/project.toml
# actually changed, prints "already up to date" and exits 0 with no
# commit. Otherwise makes exactly ONE commit and pushes (unless
# --no-push). Never touches anything else: open PRs, choir/* task
# branches, roadmap/, skills/, project sources, or any other
# branch/untracked file in the clone.
#
# Note on failure mid-script: if a later step (e.g. the pin write)
# aborts, files already rsynced in an earlier step may be left
# uncommitted in the target's working tree. This is intentional (no
# rollback machinery) — inspect/fix and re-run, or `git checkout -- .`
# in the target to discard the partial sync.

set -euo pipefail

CHOIR_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

TARGET=""
REPO=""
NO_PUSH="0"
FORCE_BUILD_WORKFLOW="0"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo)                 REPO="$2"; shift 2 ;;
    --no-push)               NO_PUSH="1"; shift ;;
    --force-build-workflow)  FORCE_BUILD_WORKFLOW="1"; shift ;;
    -*)                      echo "unknown flag: $1" >&2; exit 1 ;;
    *)                       TARGET="$1"; shift ;;
  esac
done

usage() {
  echo "usage: $0 <path-to-project-clone> [--repo <owner/name>] [--no-push] [--force-build-workflow]" >&2
}

if [[ -z "$TARGET" ]]; then
  usage
  exit 1
fi

if [[ ! -d "$TARGET" ]]; then
  echo "error: target path '$TARGET' is not a directory" >&2
  exit 1
fi
TARGET="$(cd "$TARGET" && pwd)"

# --- Preconditions -----------------------------------------------------

if ! git -C "$TARGET" rev-parse --git-dir >/dev/null 2>&1; then
  echo "error: $TARGET is not a git repository" >&2
  exit 1
fi

if [[ ! -d "$TARGET/.choir" ]]; then
  echo "error: $TARGET has no .choir/ directory — not a Choir project" >&2
  exit 1
fi

# "Clean tree" ignores untracked files (a workspace may legitimately
# have scratch/build content lying around) — it means no pending
# modifications to already-tracked files, staged or not.
if ! git -C "$TARGET" diff --quiet || ! git -C "$TARGET" diff --cached --quiet; then
  echo "error: $TARGET has uncommitted changes to tracked files — commit or stash before upgrading" >&2
  exit 1
fi

if ! CURRENT_BRANCH="$(git -C "$TARGET" symbolic-ref --short HEAD 2>/dev/null)"; then
  echo "error: $TARGET is in a detached HEAD state — checkout the default branch first" >&2
  exit 1
fi

if [[ -n "$REPO" ]]; then
  DEFAULT_BRANCH="$(gh api "repos/$REPO" --jq .default_branch)"
  if [[ "$CURRENT_BRANCH" != "$DEFAULT_BRANCH" ]]; then
    echo "error: $TARGET is on '$CURRENT_BRANCH', but $REPO's default branch is '$DEFAULT_BRANCH' — checkout it first" >&2
    exit 1
  fi
elif ORIGIN_HEAD_REF="$(git -C "$TARGET" symbolic-ref -q refs/remotes/origin/HEAD 2>/dev/null)"; then
  ORIGIN_DEFAULT_BRANCH="${ORIGIN_HEAD_REF##*/}"
  if [[ "$CURRENT_BRANCH" != "$ORIGIN_DEFAULT_BRANCH" ]]; then
    echo "error: $TARGET is on '$CURRENT_BRANCH', but origin's default branch is '$ORIGIN_DEFAULT_BRANCH' — checkout it first" >&2
    exit 1
  fi
fi
# else: no --repo and no locally-known origin/HEAD — nothing to check
# against; the currently checked-out branch is accepted as the default
# (offline best effort).

echo "==> Upgrading $TARGET (branch: $CURRENT_BRANCH)"

# --- Read the target's prover (drives verify-pr.yml/verify-trust-report.yml) --

# Reuse gate.provers.select.read_prover rather than reparsing here: it
# validates against the registry, so a typo'd prover value aborts the
# upgrade loudly instead of silently mis-selecting the workflow set.
PROVER="$(cd "$CHOIR_DIR" && TARGET_WORKSPACE="$TARGET" uv run python -c '
import os
from pathlib import Path

from gate.provers.select import read_prover

print(read_prover(Path(os.environ["TARGET_WORKSPACE"])))
')" || {
  echo "error: could not read a valid prover from $TARGET/.choir/project.toml" >&2
  exit 1
}
echo "    prover: $PROVER"

PROTOCOL_VERSION="$(cd "$CHOIR_DIR" && uv run python -c 'from gate.protocol import PROTOCOL_VERSION; print(PROTOCOL_VERSION)')"
CHOIR_COMMIT="$(git -C "$CHOIR_DIR" rev-parse HEAD)"

# --- Overlay sync --------------------------------------------------------

echo "==> Syncing overlay from $CHOIR_DIR"

mkdir -p "$TARGET/gate" "$TARGET/orchestrator" "$TARGET/client"
mkdir -p "$TARGET/.github/workflows" "$TARGET/.github/ISSUE_TEMPLATE"

rsync -a --delete --exclude '__pycache__' "$CHOIR_DIR/gate/" "$TARGET/gate/"
rsync -a --delete --exclude '__pycache__' "$CHOIR_DIR/orchestrator/" "$TARGET/orchestrator/"
rsync -a --delete --exclude '__pycache__' "$CHOIR_DIR/client/" "$TARGET/client/"
rsync -a --delete "$CHOIR_DIR/.github/ISSUE_TEMPLATE/" "$TARGET/.github/ISSUE_TEMPLATE/"

cp "$CHOIR_DIR/pyproject.toml" "$CHOIR_DIR/uv.lock" "$TARGET/"

# Project-agnostic verify workflows: no per-project templating, so a
# direct copy from this checkout is always correct.
PY_WORKFLOWS=(issue-intake verify-statement-equiv verify-statement-immutability \
              verify-axiom-honesty verify-sorry verify-style \
              verify-comparator issue-close-on-merge reconcile)
for w in "${PY_WORKFLOWS[@]}"; do
  cp "$CHOIR_DIR/templates/workflows/$w.yml" "$TARGET/.github/workflows/$w.yml"
done

OVERLAY_GIT_PATHS=(
  gate orchestrator client
  pyproject.toml uv.lock
  .github/ISSUE_TEMPLATE
  .choir/project.toml
)
for w in "${PY_WORKFLOWS[@]}"; do
  OVERLAY_GIT_PATHS+=(".github/workflows/$w.yml")
done

# The overlay set declared to the manifest is derived from the SAME array
# this script copies from, so dropping a template from PY_WORKFLOWS is the
# whole act of retiring it — there is no second list to forget
# (gate/upgrade/manifest.py). Note this is the overlay SET, not "what this
# run wrote": verify-pr.yml and .choir/verify.toml are declared on every
# run even though only bootstrap (or --force-build-workflow) writes them,
# or they would report as retired on every upgrade forever.
MANIFEST_ARGS=()
for w in "${PY_WORKFLOWS[@]}"; do
  MANIFEST_ARGS+=(--managed ".github/workflows/$w.yml")
done
MANIFEST_ARGS+=(--managed pyproject.toml --managed uv.lock)
MANIFEST_ARGS+=(--seeded ".github/workflows/verify-pr.yml")
MANIFEST_ARGS+=(--seeded ".choir/verify.toml")
if [[ "$PROVER" == "lean4" ]]; then
  MANIFEST_ARGS+=(--managed ".github/workflows/verify-trust-report.yml")
fi

# verify-trust-report.yml (lean4 only): regenerated from the same
# repo-root-oriented template new-project.sh generates, NOT copied from
# this checkout's own .github/workflows/verify-trust-report.yml (which
# assumes the samples/lean4/ layout and would be wrong at a project's
# root).
if [[ "$PROVER" == "lean4" ]]; then
  cat > "$TARGET/.github/workflows/verify-trust-report.yml" <<'YAML'
name: verify-trust-report

# Environment-level trust report (design note 12 §4, autodetection per
# §4.1) — INFORMATIONAL ONLY, never a merge gate. Rebuilds the project,
# then diffs the PR's base SHA against the checkout to find
# changed/new declarations (gate/verify/changed_decls.py) and probes
# each one via the effective prover's own kernel (`#print axioms` for
# lean4, `Print Assumptions` for rocq, kernel oracle-tracking for
# isabelle). Exits 0 whether or not any declaration turns out to
# depend on axioms/oracles, and whether or not any declaration is
# unresolvable — this check reports, it never blocks.
#
# `actions/checkout` defaults to a shallow single-commit checkout —
# `fetch-depth: 0` here so the base SHA is reachable by `git diff`/
# `git show` in detection.

on:
  pull_request:
    types: [opened, synchronize, reopened]

permissions:
  contents: read

concurrency:
  group: choir-verify-trust-report-${{ github.event.pull_request.number }}
  cancel-in-progress: true

jobs:
  trust-report:
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: Install elan
        run: |
          set -euo pipefail
          curl -sSf https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh \
            | sh -s -- -y --default-toolchain none
          echo "$HOME/.elan/bin" >> "$GITHUB_PATH"

      - name: Cache Lake artifacts and toolchain
        uses: actions/cache@v4
        with:
          path: |
            .lake
            ~/.elan
          key: lake-${{ runner.os }}-${{ hashFiles('lean-toolchain', 'lake-manifest.json', 'lakefile.toml') }}
          restore-keys: |
            lake-${{ runner.os }}-

      - name: Fetch Mathlib build cache (no-op for non-Mathlib projects)
        run: lake exe cache get || echo "::notice::no Mathlib cache target — building from source"

      # continue-on-error keeps this check honest: it is informational,
      # and a broken build is verify-pr/rebuild's failure to report.
      - name: Lake build (probes need compiled imports)
        id: build
        continue-on-error: true
        run: lake build

      - name: Install uv
        uses: astral-sh/setup-uv@v3
        with:
          enable-cache: true

      - name: Install Python deps
        run: uv sync --extra dev

      - name: Run trust report (informational; autodetects changed declarations)
        if: steps.build.outcome == 'success'
        run: |
          uv run python -m gate.verify.trust_report_cli \
            --workspace . \
            --base-sha ${{ github.event.pull_request.base.sha }}

      - name: Skip note (build failed upstream)
        if: steps.build.outcome == 'failure'
        run: echo "build failed — skipping trust probe; see verify-pr / rebuild for the real failure"
YAML
  OVERLAY_GIT_PATHS+=(.github/workflows/verify-trust-report.yml)
fi

# verify-pr.yml: protected by default.
if [[ "$FORCE_BUILD_WORKFLOW" == "1" ]]; then
  echo "==> Regenerating verify-pr.yml (--force-build-workflow)"
  case "$PROVER" in
    lean4)
      cat > "$TARGET/.github/workflows/verify-pr.yml" <<'YAML'
name: verify-pr

on:
  pull_request:
    types: [opened, synchronize, reopened]

permissions:
  contents: read

concurrency:
  group: choir-verify-pr-${{ github.event.pull_request.number }}
  cancel-in-progress: true

jobs:
  rebuild:
    runs-on: ubuntu-latest
    timeout-minutes: 90
    steps:
      - uses: actions/checkout@v4

      - name: Install elan
        run: |
          set -euo pipefail
          curl -sSf https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh \
            | sh -s -- -y --default-toolchain none
          echo "$HOME/.elan/bin" >> "$GITHUB_PATH"

      - name: Cache Lake artifacts and toolchain
        uses: actions/cache@v4
        with:
          path: |
            .lake
            ~/.elan
          key: lake-${{ runner.os }}-${{ hashFiles('lean-toolchain', 'lake-manifest.json', 'lakefile.toml') }}
          restore-keys: |
            lake-${{ runner.os }}-

      - name: Fetch Mathlib build cache (no-op for non-Mathlib projects)
        run: lake exe cache get || echo "::notice::no Mathlib cache target — building from source"

      - name: Lake build (clean-room rebuild)
        run: lake build
YAML
      ;;
    isabelle|rocq)
      # Best-effort: regenerates using Choir's default toolchain pin
      # (does not attempt to recover a project-specific pin from the
      # existing file). Isabelle/rocq templates remain UNVALIDATED
      # (design note 12 §9).
      if [[ "$PROVER" == "isabelle" ]]; then
        CHOIR_TOOLCHAIN="Isabelle2025"
      else
        CHOIR_TOOLCHAIN="9.0.0"
      fi
      echo "NOTE: regenerated verify-pr.yml for prover '$PROVER' using Choir's" >&2
      echo "      default toolchain pin ($CHOIR_TOOLCHAIN) — this does NOT preserve" >&2
      echo "      a project-specific pin; check it and adjust if needed (isabelle/" >&2
      echo "      rocq templates are UNVALIDATED, design note 12 §9)." >&2
      if [[ "$PROVER" == "isabelle" ]]; then
        cat > "$TARGET/.github/workflows/verify-pr.yml" <<'YAML'
# UNVALIDATED template (design note 12 §9): exercised at the first isabelle sample project.
name: verify-pr

on:
  pull_request:
    types: [opened, synchronize, reopened]

permissions:
  contents: read

concurrency:
  group: choir-verify-pr-${{ github.event.pull_request.number }}
  cancel-in-progress: true

jobs:
  rebuild:
    runs-on: ubuntu-latest
    timeout-minutes: 90
    env:
      ISABELLE_VERSION: "__CHOIR_TOOLCHAIN__"
    steps:
      - uses: actions/checkout@v4

      - name: Download pinned Isabelle (${{ env.ISABELLE_VERSION }})
        run: |
          set -euo pipefail
          curl -sSfL "https://isabelle.in.tum.de/dist/${ISABELLE_VERSION}_linux.tar.gz" \
            -o isabelle.tar.gz
          tar -xzf isabelle.tar.gz
          echo "$PWD/${ISABELLE_VERSION}/bin" >> "$GITHUB_PATH"

      - name: Cache Isabelle heaps
        uses: actions/cache@v4
        with:
          path: ~/.isabelle
          key: isabelle-${{ runner.os }}-${{ env.ISABELLE_VERSION }}-${{ hashFiles('ROOT', 'ROOTS') }}
          restore-keys: |
            isabelle-${{ runner.os }}-${{ env.ISABELLE_VERSION }}-

      - name: Isabelle build (clean-room rebuild)
        run: isabelle build -D .
YAML
      else
        cat > "$TARGET/.github/workflows/verify-pr.yml" <<'YAML'
# UNVALIDATED template (design note 12 §9): exercised at the first rocq sample project.
name: verify-pr

on:
  pull_request:
    types: [opened, synchronize, reopened]

permissions:
  contents: read

concurrency:
  group: choir-verify-pr-${{ github.event.pull_request.number }}
  cancel-in-progress: true

jobs:
  rebuild:
    runs-on: ubuntu-latest
    timeout-minutes: 90
    env:
      ROCQ_VERSION: "__CHOIR_TOOLCHAIN__"
    steps:
      - uses: actions/checkout@v4

      - uses: ocaml/setup-ocaml@v3
        with:
          ocaml-compiler: "5.1"

      - name: Install pinned Rocq (${{ env.ROCQ_VERSION }}) via opam
        run: opam install -y "rocq-prover.${{ env.ROCQ_VERSION }}" dune

      # Classic coq_makefile flow, if a project doesn't adopt dune:
      #
      #   - name: Rebuild via coq_makefile (alternative to dune)
      #     run: |
      #       coq_makefile -f _CoqProject -o Makefile
      #       make -j"$(nproc)"

      - name: Dune build (clean-room rebuild)
        run: opam exec -- dune build
YAML
      fi
      WORKFLOW_TEXT="$(cat "$TARGET/.github/workflows/verify-pr.yml")"
      printf '%s\n' "${WORKFLOW_TEXT//__CHOIR_TOOLCHAIN__/$CHOIR_TOOLCHAIN}" > "$TARGET/.github/workflows/verify-pr.yml"
      ;;
    *)
      echo "error: unknown prover '$PROVER' — cannot regenerate verify-pr.yml" >&2
      exit 1
      ;;
  esac
  OVERLAY_GIT_PATHS+=(.github/workflows/verify-pr.yml)
else
  echo "NOTE: verify-pr.yml left untouched (overseer-adapted) — pass --force-build-workflow to overwrite it with Choir's current template."
fi

# --- Labels -----------------------------------------------------------------

if [[ -n "$REPO" ]]; then
  echo "==> Seeding lifecycle labels on $REPO"
  "$CHOIR_DIR/scripts/setup-labels.sh" "$REPO"

  # Required-contexts hygiene. REPORT ONLY, by design: repo settings have no
  # git history to revert and the protection endpoint replaces the whole
  # object, so writing here could silently reset enforce_admins/strict/review
  # requirements the overseer set deliberately. gate/checks.py holds the one
  # copy of the expected names.
  echo "==> Checking branch protection required contexts on $REPO"
  if LIVE_CONTEXTS="$(gh api "repos/$REPO/branches/$DEFAULT_BRANCH/protection" \
      --jq '.required_status_checks.contexts[]?' 2>/dev/null)"; then
    if ! CONTEXT_REPORT="$(printf '%s\n' "$LIVE_CONTEXTS" \
        | (cd "$CHOIR_DIR" && uv run python -c '
import sys
from gate.checks import BRANCH_PROTECTION_CONTEXTS
live = {line.strip() for line in sys.stdin if line.strip()}
want = set(BRANCH_PROTECTION_CONTEXTS)
for name in sorted(live - want):
    print(f"no longer shipped: {name}")
for name in sorted(want - live):
    print(f"now required but absent: {name}")
'))"; then
      CONTEXT_REPORT=""
      echo "    WARNING: could not compare contexts (skipped)"
    fi
    if [[ -n "$CONTEXT_REPORT" ]]; then
      echo "    NOTE: required contexts differ from this Choir's set:"
      echo "$CONTEXT_REPORT" | sed 's/^/      /'
      echo "    This script does NOT change protection. Adjust it yourself if"
      echo "    you want it aligned — a context naming a check that no longer"
      echo "    reports will block merges at the GitHub layer wherever"
      echo "    enforce_admins is true."
    else
      echo "    required contexts match"
    fi
  else
    echo "    protection not enabled (or not readable) — skipping. Note that"
    echo "    merge_pr's preflight, not protection, is what enforces the gate."
  fi
else
  echo "skip: labels not seeded (no --repo given) — run scripts/setup-labels.sh <owner/name> manually if needed"
fi

# --- Pin ---------------------------------------------------------------

echo "==> Writing protocol pin (protocol $PROTOCOL_VERSION, choir ${CHOIR_COMMIT:0:7})"
PIN_RESULT="$(cd "$CHOIR_DIR" && uv run python -m gate.upgrade.pin "$TARGET/.choir/project.toml" --protocol "$PROTOCOL_VERSION" --commit "$CHOIR_COMMIT")"
echo "    project.toml pin: $PIN_RESULT"

# --- Retire files this Choir no longer ships ---------------------------------

# Derived retirement (gate/upgrade/manifest.py): paths a previous run
# installed and this one no longer declares. A `managed` file whose content
# still matches what Choir wrote is deleted; anything edited locally, and
# anything `seeded`, is reported and left alone. Runs AFTER every install so
# the recorded hashes are the hashes of what is now on disk.
echo "==> Reconciling the overlay manifest"
MANIFEST_OUT="$(cd "$CHOIR_DIR" && uv run python -m gate.upgrade.manifest \
  "$TARGET" "${MANIFEST_ARGS[@]}")" || {
  echo "error: overlay manifest reconciliation failed — nothing committed" >&2
  exit 1
}
if [[ -n "$MANIFEST_OUT" ]]; then
  echo "$MANIFEST_OUT" | sed 's/^/    /'
  # Herestring, not a pipe: a pipe would run the loop in a subshell and the
  # OVERLAY_GIT_PATHS appends would be lost, leaving the deletions unstaged.
  while IFS=$'\t' read -r kind path _rest; do
    if [[ "$kind" == "deleted" ]]; then
      OVERLAY_GIT_PATHS+=("$path")
    fi
  done <<< "$MANIFEST_OUT"
fi
OVERLAY_GIT_PATHS+=(.choir/overlay.json)

# --- Idempotency check + commit + push --------------------------------------

git -C "$TARGET" add -- "${OVERLAY_GIT_PATHS[@]}"
CHANGED_SUMMARY="$(git -C "$TARGET" diff --cached --name-status)"

if [[ -z "$CHANGED_SUMMARY" ]]; then
  echo "already up to date"
  exit 0
fi

echo "==> Changed paths:"
echo "$CHANGED_SUMMARY" | sed 's/^/    /'

git -C "$TARGET" commit -q -m "choir: upgrade overlay to protocol $PROTOCOL_VERSION (choir ${CHOIR_COMMIT:0:7})"
echo "==> Committed upgrade on $CURRENT_BRANCH"

if [[ "$NO_PUSH" == "1" ]]; then
  echo "==> --no-push given: commit created locally, not pushed"
else
  echo "==> Pushing"
  git -C "$TARGET" push
fi
