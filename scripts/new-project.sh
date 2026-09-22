#!/usr/bin/env bash
# new-project.sh — bootstrap a Choir project repo (the general, mechanical part).
#
# Usage:
#   scripts/new-project.sh <owner/name> [--from <git-url>] [--toolchain <pin>]
#                           [--public] [--prover lean4|isabelle|rocq]
#
#   --from <git-url>    start from an existing repo in the target prover
#                       (its own toolchain pin, e.g. lean-toolchain, is
#                       kept). Without --from, a minimal fresh skeleton is
#                       created for the selected --prover.
#   --toolchain <pin>   fresh mode only: the toolchain to pin. Meaning is
#                       per-prover: lean4 wants e.g. "leanprover/lean4:v4.28.0"
#                       (default: latest stable Lean release, resolved now);
#                       isabelle wants an Isabelle release tag, e.g.
#                       "Isabelle2025" (default: "Isabelle2025"); rocq wants
#                       an opam `rocq-prover` version, e.g. "9.0.0" (default:
#                       "9.0.0"). Per the Choir pinned-versions rule, projects
#                       pin, never float.
#   --public            create the repo public (default: private).
#   --prover <p>        lean4 | isabelle | rocq (default: lean4). Selects the
#                       skeleton and the verify-pr.yml rebuild template
#                       written below; recorded as `[project] prover` in the
#                       generated `.choir/project.toml` (design note 12 §7).
#                       Isabelle/rocq skeletons and workflow templates are
#                       UNVALIDATED until the samples slice (note 12 §9).
#
# This script hard-codes only what is the same for every Choir project:
#   1. The Lean project base (cloned or skeleton).
#   2. The Choir overlay: gate/ orchestrator/ client/ + pyproject + uv.lock,
#      the gate workflows + issue template, and a root-level rebuild workflow.
#   3. .choir/ policy files with the GENERAL defaults (automation=auto,
#      axiom policy=net_zero). Project-specific policy — whitelists, approve
#      mode, skill packs, build tweaks — is the overseer's / orchestrator's
#      job AFTER bootstrap; see docs/agents/orchestrator-setup.md "Bootstrapping
#      or resuming a project".
#   4. Repo creation + push, lifecycle labels, branch-protection attempt.
#
# Idempotency: refuses to run if the target repo already exists — resume it
# instead (docs/agents/orchestrator-setup.md).

set -euo pipefail

CHOIR_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Protocol pin (design note 13 §3) — written into the generated
# .choir/project.toml below, at bootstrap time. Computed once, up
# front, so later `cd`s into the fresh $PROJECT don't matter.
CHOIR_PROTOCOL_VERSION="$(cd "$CHOIR_DIR" && uv run python -c 'from gate.protocol import PROTOCOL_VERSION; print(PROTOCOL_VERSION)')"
CHOIR_BOOTSTRAP_COMMIT="$(git -C "$CHOIR_DIR" rev-parse HEAD)"

TARGET=""
SOURCE_URL=""
TOOLCHAIN=""
VISIBILITY="--private"
PROVER="lean4"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --from)      SOURCE_URL="$2"; shift 2 ;;
    --toolchain) TOOLCHAIN="$2"; shift 2 ;;
    --public)    VISIBILITY="--public"; shift ;;
    --prover)    PROVER="$2"; shift 2 ;;
    -*)          echo "unknown flag: $1" >&2; exit 1 ;;
    *)           TARGET="$1"; shift ;;
  esac
done

usage() {
  echo "usage: $0 <owner/name> [--from <git-url>] [--toolchain <pin>] [--public] [--prover lean4|isabelle|rocq]" >&2
}

if [[ -z "$TARGET" ]]; then
  usage
  exit 1
fi

case "$PROVER" in
  lean4|isabelle|rocq) ;;
  *)
    echo "unknown --prover: $PROVER (expected one of: lean4, isabelle, rocq)" >&2
    usage
    exit 1
    ;;
esac

# Isabelle/rocq default toolchain pin, resolved whenever --toolchain is
# omitted — independent of --from vs fresh-skeleton mode, because the
# verify-pr.yml template bakes this value in regardless of where the
# project source comes from (design note 12 §7, §2.2: neither prover
# has a lean-toolchain-style repo file to pin via instead). lean4's
# default is resolved later, fresh-skeleton-mode only: it needs a
# network call, and --from mode never needs a bootstrap-time value at
# all — the cloned repo already carries its own lean-toolchain file.
COMPARATOR_REPO="https://github.com/leanprover/comparator"

comparator_has_tag() {
  # A tag the comparator build can actually check out. Release candidates
  # are tagged too, so an rc pin resolves exactly.
  [ -n "${1:-}" ] || return 1
  git ls-remote --exit-code --tags "$COMPARATOR_REPO" "refs/tags/$1" \
    >/dev/null 2>&1
}

newest_comparator_release() {
  git ls-remote --tags "$COMPARATOR_REPO" 2>/dev/null \
    | awk '{print $2}' | sed 's#refs/tags/##' \
    | grep -vE '\^\{\}$|-rc' | sort -V | tail -1
}

if [[ -z "$TOOLCHAIN" ]]; then
  case "$PROVER" in
    isabelle) TOOLCHAIN="Isabelle2025"; echo "==> Pinning Isabelle: $TOOLCHAIN" ;;
    rocq)     TOOLCHAIN="9.0.0";        echo "==> Pinning Rocq: $TOOLCHAIN" ;;
  esac
fi

if gh repo view "$TARGET" >/dev/null 2>&1; then
  echo "error: $TARGET already exists — resume it instead of re-bootstrapping." >&2
  echo "       (docs/agents/orchestrator-setup.md, 'Bootstrapping or resuming a project')" >&2
  exit 1
fi

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT
PROJECT="$WORKDIR/project"

# Library/session name from the repo name (alphanumerics, capitalized). Names
# the source subfolder in the fresh-skeleton branches below, so formalization
# files live in a named folder rather than loose at the repo root.
NAME="$(basename "$TARGET" | tr -cd '[:alnum:]')"
LIB="$(tr '[:lower:]' '[:upper:]' <<< "${NAME:0:1}")${NAME:1}"

if [[ -n "$SOURCE_URL" ]]; then
  echo "==> Cloning $SOURCE_URL"
  git clone --depth 1 "$SOURCE_URL" "$PROJECT"
  rm -rf "$PROJECT/.git"
elif [[ "$PROVER" == "lean4" ]]; then
  echo "==> Creating fresh Lake skeleton"
  if [[ -z "$TOOLCHAIN" ]]; then
    TAG="$(gh api repos/leanprover/lean4/releases/latest --jq .tag_name)"
    # `verify-comparator` builds the comparator at the tag matching this
    # pin, and comparator tags only `.0` releases — there is no
    # v4.33.1. Pinning Lean's newest patch release therefore leaves the
    # kernel statement gate unable to run on every PR in the project, and
    # the pin cannot be changed later (pinned versions are a
    # non-negotiable). So default to the newest release comparator has.
    if ! comparator_has_tag "$TAG"; then
      SUPPORTED="$(newest_comparator_release || true)"
      if [[ -n "$SUPPORTED" ]]; then
        echo "    note: comparator has no tag for $TAG (it tags only .0"
        echo "          releases), so the kernel statement gate could not run."
        echo "          Pinning $SUPPORTED instead — pass --toolchain to override."
        TAG="$SUPPORTED"
      else
        echo "    warning: could not reach comparator's tags to check $TAG"
      fi
    fi
    TOOLCHAIN="leanprover/lean4:$TAG"
    echo "    pinning: $TOOLCHAIN"
  elif ! comparator_has_tag "${TOOLCHAIN##*:}"; then
    echo "    warning: comparator has no tag for ${TOOLCHAIN##*:}, so"
    echo "             verify-comparator cannot run in this project. It tags"
    echo "             only .0 releases; $(newest_comparator_release) is the newest it has."
  fi
  mkdir -p "$PROJECT"
  printf '%s\n' "$TOOLCHAIN" > "$PROJECT/lean-toolchain"
  cat > "$PROJECT/lakefile.toml" <<TOML
name = "$LIB"
version = "0.1.0"
defaultTargets = ["$LIB"]

[[lean_lib]]
name = "$LIB"
TOML
  # Lake convention: root module `$LIB.lean` re-exports the library, whose
  # modules live under `$LIB/`. Keep sources in that folder, not at the root.
  mkdir -p "$PROJECT/$LIB"
  printf 'import %s.Basic\n' "$LIB" > "$PROJECT/$LIB.lean"
  printf 'def hello : String := "choir"\n' > "$PROJECT/$LIB/Basic.lean"
  printf '.lake/\n__pycache__/\n' > "$PROJECT/.gitignore"
elif [[ "$PROVER" == "isabelle" ]]; then
  # UNVALIDATED template (design note 12 §9): exercised at the first
  # isabelle sample project. Minimal enough that the repo is coherent;
  # no elan-equivalent toolchain resolution — Isabelle pins per design
  # note 12 §2.2 via project_ref.toolchain, not a repo-committed file.
  echo "==> Creating minimal Isabelle skeleton (UNVALIDATED until the samples slice)"
  mkdir -p "$PROJECT/$LIB"
  # ROOT stays at the repo root; the `in "$LIB"` clause puts the session's
  # theories in the $LIB/ subfolder, so `isabelle build -D .` still selects
  # the session. Unquoted heredoc so $LIB expands (no other $ or backticks).
  cat > "$PROJECT/ROOT" <<ISAROOT
(* UNVALIDATED template (design note 12 §9): exercised at the first isabelle sample project. *)
session $LIB in "$LIB" = HOL +
  theories
    Sample
ISAROOT
  cat > "$PROJECT/$LIB/Sample.thy" <<'ISATHY'
(* UNVALIDATED template (design note 12 §9): exercised at the first isabelle sample project. *)
theory Sample
  imports Main
begin

lemma choir_placeholder: "True"
  by simp

end
ISATHY
  printf 'heaps/\nbrowser_info/\n__pycache__/\n' > "$PROJECT/.gitignore"
else
  # UNVALIDATED template (design note 12 §9): exercised at the first
  # rocq sample project. Minimal enough that the repo is coherent; no
  # elan-equivalent toolchain resolution — Rocq pins per design note
  # 12 §2.2 via project_ref.toolchain, not a repo-committed file.
  echo "==> Creating minimal Rocq skeleton (UNVALIDATED until the samples slice)"
  mkdir -p "$PROJECT/$LIB"
  cat > "$PROJECT/dune-project" <<'DUNEPROJ'
; UNVALIDATED template (design note 12 §9): exercised at the first rocq sample project.
(lang dune 3.0)
(using coq 0.8)
DUNEPROJ
  # _CoqProject maps the $LIB/ subfolder to logical name $LIB; unquoted
  # heredoc so $LIB expands.
  cat > "$PROJECT/_CoqProject" <<COQPROJECT
# UNVALIDATED template (design note 12 §9): exercised at the first rocq sample project.
-Q $LIB $LIB
$LIB/Sample.v
COQPROJECT
  # dune file lives beside the sources it builds ($LIB/); without a coq.theory
  # stanza dune build compiles no .v files, so verify-pr would pass vacuously.
  cat > "$PROJECT/$LIB/dune" <<DUNE
; UNVALIDATED template (design note 12 §9): exercised at the first rocq sample project.
(coq.theory
 (name $LIB))
DUNE
  cat > "$PROJECT/$LIB/Sample.v" <<'ROCQV'
(* UNVALIDATED template (design note 12 §9): exercised at the first rocq sample project. *)
Theorem choir_placeholder : True.
Proof. exact I. Qed.
ROCQV
  printf '_build/\n__pycache__/\n' > "$PROJECT/.gitignore"
fi

cd "$PROJECT"
git init -q -b main
PROJECT_ROOT="$(pwd)"

echo "==> Overlaying Choir"
# rsync --exclude, not `cp -r`: a stale __pycache__ shell on the Choir
# checkout (e.g. gate/review/__pycache__, orphaned by a retired package)
# would otherwise get committed into a brand-new project — matches
# upgrade-project.sh's existing `--exclude '__pycache__'` discipline.
rsync -a --exclude '__pycache__' "$CHOIR_DIR/gate/" gate/
rsync -a --exclude '__pycache__' "$CHOIR_DIR/orchestrator/" orchestrator/
rsync -a --exclude '__pycache__' "$CHOIR_DIR/client/" client/
cp "$CHOIR_DIR/pyproject.toml" "$CHOIR_DIR/uv.lock" .

mkdir -p .github/workflows
# One array: the copy loop below and the overlay-manifest declaration near
# the first commit both iterate it, so a workflow added here is installed
# AND declared, with no second list to forget.
PY_WORKFLOWS=(issue-intake verify-statement-equiv verify-statement-immutability \
              verify-axiom-honesty verify-sorry verify-style \
              verify-comparator issue-close-on-merge reconcile)
for w in "${PY_WORKFLOWS[@]}"; do
  cp "$CHOIR_DIR/templates/workflows/$w.yml" .github/workflows/
done
cp -r "$CHOIR_DIR/.github/ISSUE_TEMPLATE" .github/

# Root-level rebuild workflow, dispatched on --prover. lean4: elan from
# the project's own lean-toolchain, cached artifacts, `lake build` at
# the repo root (unchanged from before --prover existed). isabelle/rocq:
# UNVALIDATED templates (design note 12 §9) — documented recipes, not
# yet exercised against a live toolchain; flagged in the file header.
# The orchestrator reviews and adapts this after bootstrap if the
# project needs more (extra targets, larger runners, ...).
if [[ "$PROVER" == "lean4" ]]; then
  # The Mathlib cache step is a guarded no-op for projects that don't
  # depend on Mathlib.
  cat > .github/workflows/verify-pr.yml <<'YAML'
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

  # Informational (never a merge gate) trust report, autodetecting
  # changed declarations off the PR's base SHA (design note 12 §4.1).
  # Same shape as the Choir repo's own .github/workflows/
  # verify-trust-report.yml, adapted so --workspace is the project
  # repo root (not samples/lean4) — this project's own root IS its
  # Lake project. `fetch-depth: 0` so the base SHA is reachable by
  # git diff/show in detection.
  cat > .github/workflows/verify-trust-report.yml <<'YAML'
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
elif [[ "$PROVER" == "isabelle" ]]; then
  # Fully-quoted heredoc, written directly to the file (no bash expansion
  # of the GitHub Actions ${{ ... }} expressions or the runtime
  # $GITHUB_PATH/$HOME refs); __CHOIR_TOOLCHAIN__ is the only
  # substitution, applied afterward via bash parameter expansion on the
  # file contents. Deliberately NOT `VAR="$(cat <<'YAML' ... )"` — a
  # heredoc nested inside a `$(...)` command substitution mis-parses an
  # apostrophe in a comment (bash treats it as an unterminated quote
  # even though the heredoc delimiter is quoted) — write-then-read-back
  # avoids that gotcha.
  cat > .github/workflows/verify-pr.yml <<'YAML'
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
  cat > .github/workflows/verify-trust-report.yml <<'YAML'
# UNVALIDATED template (design note 12 §9): exercised at the first isabelle sample project.
name: verify-trust-report

# Environment-level trust report (design note 12 §4) — INFORMATIONAL ONLY,
# never a merge gate. Rebuilds the session, then probes changed declarations
# via Isabelle kernel oracle-tracking (thm_oracles). Exits 0 regardless; if
# the rebuild fails, the probe step is skipped, so this workflow stays quiet
# on a tree that does not build. fetch-depth: 0 so the base SHA is reachable
# by git diff in changed-declaration detection.
#
# The build step is a GATE, not a prerequisite: the probe loads the theories
# it needs from SOURCE (`Thy_Info.use_theories`) and needs no heap image, so
# `isabelle build -D .` without `-b` is adequate — it is here only so the
# report is not computed against a tree that does not compile.
#
# The probe recipe is validated against a live Isabelle2025-2; this
# TEMPLATE has never run, since no isabelle project exists yet.

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
    timeout-minutes: 90
    env:
      ISABELLE_VERSION: "__CHOIR_TOOLCHAIN__"
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

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

      - name: Isabelle build (gate: do not probe a tree that does not compile)
        id: build
        continue-on-error: true
        run: isabelle build -D .

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

      - name: Skip note (rebuild failed upstream)
        if: steps.build.outcome == 'failure'
        run: echo "::notice::trust-report skipped — the rebuild failed (see verify-pr)."
YAML
  # Substitute the pinned toolchain into both generated workflows.
  for _wf in verify-pr verify-trust-report; do
    _content="$(cat ".github/workflows/$_wf.yml")"
    printf '%s\n' "${_content//__CHOIR_TOOLCHAIN__/$TOOLCHAIN}" > ".github/workflows/$_wf.yml"
  done
else
  # Same write-then-substitute approach as isabelle above.
  cat > .github/workflows/verify-pr.yml <<'YAML'
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
  cat > .github/workflows/verify-trust-report.yml <<'YAML'
# UNVALIDATED template (design note 12 §9): exercised at the first rocq sample project.
name: verify-trust-report

# Environment-level trust report (design note 12 §4) — INFORMATIONAL ONLY,
# never a merge gate. Rebuilds the project, then probes changed declarations
# via Rocq Print Assumptions. Exits 0 regardless; if the (still-unvalidated)
# rebuild fails, the probe step is skipped, so this workflow stays quiet
# until the build is validated. fetch-depth: 0 so the base SHA is reachable
# by git diff in changed-declaration detection.

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
    timeout-minutes: 90
    env:
      ROCQ_VERSION: "__CHOIR_TOOLCHAIN__"
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - uses: ocaml/setup-ocaml@v3
        with:
          ocaml-compiler: "5.1"

      - name: Install pinned Rocq (${{ env.ROCQ_VERSION }}) via opam
        run: opam install -y "rocq-prover.${{ env.ROCQ_VERSION }}" dune

      - name: Dune build (probes need compiled imports)
        id: build
        continue-on-error: true
        run: opam exec -- dune build

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

      - name: Skip note (rebuild failed upstream)
        if: steps.build.outcome == 'failure'
        run: echo "::notice::trust-report skipped — the rebuild failed (see verify-pr)."
YAML
  # Substitute the pinned toolchain into both generated workflows.
  for _wf in verify-pr verify-trust-report; do
    _content="$(cat ".github/workflows/$_wf.yml")"
    printf '%s\n' "${_content//__CHOIR_TOOLCHAIN__/$TOOLCHAIN}" > ".github/workflows/$_wf.yml"
  done
fi

# Privacy guard: the overseer's local config belongs in ~/.choir/ on
# their machine, never in the repo. Gitignore it in case one is ever
# created in the wrong place.
touch .gitignore
printf '\n# Choir: overseer-local config never belongs in the repo\n.choir/orchestrator.toml\n' >> .gitignore
# `choir orch viz` writes its page into the project folder. It is an
# artefact of reading the history, not part of it.
printf 'choir-proof-tree.html\n' >> .gitignore

mkdir -p .choir
cat > .choir/project.toml <<TOML
# Prover selection — read gate-side (base-SHA) and worker-side (pinned
# commit) by gate/provers/select.py. lean4 | isabelle | rocq; see
# design note 12 in the Choir repo.
[project]
prover = "$PROVER"

# Protocol pin — the wire-protocol version + Choir commit this overlay
# was written from (design note 13 §3). Written by bootstrap/upgrade
# only, never by hand; read gate-side (gate/protocol.py) and enforced
# client-side at claim time.
choir_protocol = $CHOIR_PROTOCOL_VERSION
choir_commit = "$CHOIR_BOOTSTRAP_COMMIT"

# Automation level — read by the orchestrator agent at loop start.
# auto | approve | manual. See docs/agents/ORCHESTRATOR.md in the Choir repo.
[automation]
merge = "auto"

# How long a claim may go without a heartbeat before the daily reconcile
# workflow reclaims it (gate/reconcile/config.py). Tune to project pace:
# short for fast benchmarks, long for multi-month formalizations. Default 7.
[reconcile]
stale_after_days = 7
TOML
cat > .choir/verify.toml <<'TOML'
# Axiom policy — read by verify-axiom-honesty from the BASE commit of
# each PR (a PR cannot loosen its own policy).
#
# net_zero (default): PRs must not increase the prover's trust-surface
#   counts (its axioms + the prover-specific trust tokens, per
#   gate/provers/) relative to base.
# whitelist: only the listed names are permitted anywhere in changed
#   files. Uncomment and edit if this project demands it (lean4 shown):
#
# [audits.axiom_honesty]
# policy = "whitelist"
# allowed_axioms = ["propext", "Quot.sound", "Classical.choice"]

# Style audit — max lines per top-level declaration before verify-style
# flags it (read from the base commit). Default 200; raise it for domains
# with genuinely long proofs (heavy algebra/combinatorics), lower it for
# repos that want tight proofs.
[audits.style]
threshold_lines = 200
TOML

# Every project needs a README: pyproject.toml declares readme = "README.md",
# so the gate workflows' Python-package install (hatchling build) fails without
# it. A --from source repo usually ships its own; only generate when absent.
if [[ ! -f README.md ]]; then
  printf '# %s\n\nA Choir-managed formalization project.\n' "$(basename "$TARGET")" > README.md
fi

# Record the overlay install set so the FIRST upgrade-project.sh run has a
# manifest to diff against — without this it would be a seed run and could
# retire nothing (gate/upgrade/manifest.py). Derived from the same workflow
# list copied above; keep the two in step (tests/gate/test_checks.py guards it).
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
(cd "$CHOIR_DIR" && uv run python -m gate.upgrade.manifest \
  "$PROJECT_ROOT" "${MANIFEST_ARGS[@]}") >/dev/null

git add -A
git commit -q -m "Bootstrap Choir project${SOURCE_URL:+ from $SOURCE_URL}"

echo "==> Creating $TARGET ($VISIBILITY) and pushing"
gh repo create "$TARGET" "$VISIBILITY" --source . --push

echo "==> Seeding lifecycle labels"
"$CHOIR_DIR/scripts/setup-labels.sh" "$TARGET"

echo "==> Attempting branch protection (required gate checks, owner exempt)"
# gate/checks.py is the authority for what each context blocks on; the
# array below is pinned against it by tests/gate/test_checks.py. Two
# entries that look odd are deliberate: "style" is ADVISORY (spec D9) and
# always passes, so listing it is harmless and dropping it would be a
# no-op; "comparator" is listed because its workflow ships for every prover
# and reports a green not-applicable line on non-lean4 provers and
# sub-v4.27 toolchains, so requiring it false-blocks nobody while a PR that
# deletes verify-comparator.yml can no longer read clean.
#
# "enforce_admins": false is spec D4, and a deliberate hole: under D4 only
# the owner has write access — contributors fork and open PRs — so
# protection has nobody left to constrain except the owner, who is exactly
# who must push directly to this branch (the blueprint, skills/,
# orchestrator-notes.md).
if gh api -X PUT "repos/$TARGET/branches/main/protection" \
    --input - >/dev/null 2>&1 <<'JSON'
{
  "required_status_checks": {
    "strict": false,
    "contexts": ["rebuild", "statement-equiv",
                 "axiom-honesty", "sorry-delta", "style",
                 "comparator", "statement-immutability"]
  },
  "enforce_admins": false,
  "required_pull_request_reviews": null,
  "restrictions": null
}
JSON
then
  echo "    protection enabled. After the FIRST PR runs, verify the required"
  echo "    context names match what GitHub reports (gh pr checks <n>) and"
  echo "    adjust the rule if they differ."
else
  echo "WARNING: branch protection failed — likely a private repo without"
  echo "    GitHub Pro. The run still works; see the note below, which"
  echo "    applies either way. Options: --public, or upgrade the plan."
fi

echo
echo "==> Two things to know about the gate (docs/agents/ORCHESTRATOR.md has the why)"
echo "    Protection is set with enforce_admins: false (spec D4), so GitHub"
echo "    enforces nothing against you. What enforces \"never merge red\" is"
echo "    merge_pr's own preflight (orchestrator/prs.py), so ALWAYS MERGE"
echo "    THROUGH THE TOOLKIT — the web UI and a bare 'gh pr merge' both"
echo "    bypass it. And merge_pr's force= override is yours, the"
echo "    overseer's; the orchestrator may not use it on its own judgment."

echo
echo "==> Fork-PR workflow approval (contributors submit from forks)"
# Every contribution arrives from a fork under spec D4, and GitHub gates
# fork-PR workflow runs. A held run means the PR shows NO checks, which
# merge_pr correctly refuses — so this is not a safety hole, it is a queue
# that stops moving. The two visibilities have different knobs.
if [[ "$VISIBILITY" == "--private" ]]; then
echo "    THIS REPO IS PRIVATE, which matters twice. A contributor needs read"
echo "    access to see or fork it at all (use --public for genuinely open"
echo "    contribution). And GitHub does not run fork-PR workflows on private"
echo "    repos by default: enable Settings > Actions > General > 'Fork pull"
echo "    request workflows from outside collaborators', or every fork PR"
echo "    arrives with NO checks — which fails safe, since merge_pr refuses a"
echo "    PR with no checks, but refuses every contribution until it is on."
else
  # Three policies exist and none of them is "never require approval", so
  # approval can be narrowed but not switched off. The most permissive holds
  # runs only for accounts new to GitHub itself; the GitHub default,
  # `first_time_contributors`, holds the first PR of every contributor who
  # has not landed a commit here — i.e. precisely everyone open contribution
  # is meant to attract.
  #
  # Loosening this does not expose credentials: a fork-PR run gets a
  # read-only GITHUB_TOKEN and no secrets, and Choir's one
  # pull_request_target workflow never checks out PR code. What it exposes
  # is Actions compute. That is the trade, stated so it can be declined.
  if gh api -X PUT "repos/$TARGET/actions/permissions/fork-pr-contributor-approval" \
      -f approval_policy=first_time_contributors_new_to_github >/dev/null 2>&1; then
    echo "    Approval policy set to 'first_time_contributors_new_to_github':"
    echo "    an established GitHub account's first PR here runs its checks"
    echo "    immediately. Accounts new to GitHub still need one approval."
    echo "    To be stricter: gh api -X PUT \\"
    echo "      repos/$TARGET/actions/permissions/fork-pr-contributor-approval \\"
    echo "      -f approval_policy=first_time_contributors"
  else
    echo "WARNING: could not set the fork-PR approval policy. The GitHub"
    echo "    default ('first_time_contributors') then applies, so every new"
    echo "    contributor's FIRST PR arrives with no checks until approved."
  fi
echo
echo "    Held runs are not a rejected PR — a PR with no checks is either one"
echo "    that deleted its workflows or one waiting on you. Tell them apart"
echo "    with orchestrator/runs.py (pending_approvals / approve_runs_for_sha);"
echo "    docs/agents/ORCHESTRATOR.md has the loop step. Anyone can also claim a task"
echo "    (a claim is just a comment): the stale-lease window and the"
echo "    reconcile sweep bound a bad actor sitting on leases, and there is"
echo "    no rate limiting."
fi

echo
echo "==> Done: https://github.com/$TARGET"
echo "    Project-specific setup is now the orchestrator's job: review"
echo "    .choir/ policy, adapt verify-pr.yml if needed, add a skills/ pack,"
echo "    publish tasks. See docs/agents/ORCHESTRATOR.md."
