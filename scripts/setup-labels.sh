#!/usr/bin/env bash
# Create the labels Choir's workflows expect, on a given repo. Idempotent:
# `--force` updates color/description if the label already exists.
#
# Usage: scripts/setup-labels.sh <owner>/<repo>

set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "usage: $0 <owner>/<repo>" >&2
  exit 1
fi

REPO="$1"

# Lifecycle labels (mutually exclusive — see AGENTS.md decisions log).
gh label create "choir/available"     --repo "$REPO" --color "0e8a16" --description "Available for claim"               --force
gh label create "choir/claimed"       --repo "$REPO" --color "fbca04" --description "Currently claimed"                 --force
gh label create "choir/in-review"     --repo "$REPO" --color "5319e7" --description "Under review"                      --force
gh label create "choir/done"          --repo "$REPO" --color "b60205" --description "Completed and merged"              --force
gh label create "choir/invalid"       --repo "$REPO" --color "999999" --description "Failed intake validation"          --force

# Identity / type tags.
gh label create "choir/task"          --repo "$REPO" --color "ededed" --description "Choir-managed task (any state)"     --force
gh label create "choir/type:prove"    --repo "$REPO" --color "1f883d" --description "Task type: prove"                  --force
gh label create "choir/type:golf"     --repo "$REPO" --color "1f883d" --description "Task type: golf"                   --force
gh label create "choir/type:index"    --repo "$REPO" --color "1f883d" --description "Task type: index"                  --force

# Priority / difficulty (orchestrator-owned; absence = normal / unrated).
gh label create "choir/priority:high"      --repo "$REPO" --color "d93f0b" --description "Claim me first"                   --force
gh label create "choir/priority:low"       --repo "$REPO" --color "c5def5" --description "Claim me last"                    --force
gh label create "choir/difficulty:easy"    --repo "$REPO" --color "bfe5bf" --description "Estimated difficulty: easy"       --force
gh label create "choir/difficulty:medium"  --repo "$REPO" --color "fef2c0" --description "Estimated difficulty: medium"     --force
gh label create "choir/difficulty:hard"    --repo "$REPO" --color "f9d0c4" --description "Estimated difficulty: hard"       --force

echo "Labels created on $REPO."
