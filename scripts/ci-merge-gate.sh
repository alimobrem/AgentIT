#!/usr/bin/env bash
# Refuse to proceed when a PR's required AgentIT CI checks are not fully green.
# Usage: ./scripts/ci-merge-gate.sh <pr-number> [owner/repo]
# Exit 0 only when test + browser-critical + image-smoke-test + scan all
# bucket=pass. Pending/queued/failing → exit 1.
set -euo pipefail

PR="${1:-}"
REPO="${2:-alimobrem/AgentIT}"
if [[ -z "$PR" ]]; then
  echo "usage: $0 <pr-number> [owner/repo]" >&2
  exit 2
fi

REQUIRED=(test browser-critical image-smoke-test scan)

JSON=$(gh pr checks "$PR" --repo "$REPO" --json name,state,bucket)
if [[ -z "$JSON" || "$JSON" == "[]" ]]; then
  echo "ERROR: no checks returned for PR #${PR} (${REPO})" >&2
  exit 1
fi

fail=0
for req in "${REQUIRED[@]}"; do
  bucket=$(echo "$JSON" | jq -r --arg n "$req" \
    '[.[] | select(.name==$n) | .bucket] | first // "missing"')
  state=$(echo "$JSON" | jq -r --arg n "$req" \
    '[.[] | select(.name==$n) | .state] | first // "missing"')
  if [[ "$bucket" == "pass" ]]; then
    echo "OK  $req state=$state bucket=$bucket"
  else
    echo "BLOCK $req state=$state bucket=$bucket (need bucket=pass; not pending/queued/fail)" >&2
    fail=1
  fi
done

if [[ "$fail" -ne 0 ]]; then
  echo "Merge gate failed for PR #${PR} — do not merge." >&2
  exit 1
fi
echo "Merge gate green for PR #${PR}."
exit 0
