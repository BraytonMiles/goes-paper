#!/usr/bin/env sh
#
# pinger.sh — force the GOES render workflow to run *now*.
#
# GitHub's scheduled cron ("*/10 * * * *" in render.yml) is best-effort: under
# load it lands 5-20 min late and sometimes skips a slot. Running this script
# from a reliable scheduler every 10 minutes makes renders actually land on
# time, so the board (which wakes every 30 min) always finds a fresh frame.
#
# It calls the workflow_dispatch API. That needs a GitHub token with Actions
# read+write on this repo, supplied via the GITHUB_TOKEN environment variable.
#
#   *** NEVER hardcode the token here or commit it. ***
#   Store it in your scheduler's secret/env field. See PINGER.md.
#
# Usage:
#   GITHUB_TOKEN=github_pat_xxx ./pinger.sh
#
# Override defaults with env vars if you fork/rename:
#   REPO=owner/repo  WORKFLOW=render.yml  REF=master
#
set -eu

REPO="${REPO:-BraytonMiles/goes-paper}"
WORKFLOW="${WORKFLOW:-render.yml}"
REF="${REF:-master}"

: "${GITHUB_TOKEN:?set GITHUB_TOKEN to a PAT with Actions read+write on ${REPO}}"

# workflow_dispatch returns 204 No Content on success; -f makes curl fail loudly
# on any 4xx/5xx (bad token, wrong ref, workflow not found).
curl -fsS -X POST \
  -H "Accept: application/vnd.github+json" \
  -H "Authorization: Bearer ${GITHUB_TOKEN}" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  "https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW}/dispatches" \
  -d "{\"ref\":\"${REF}\"}"

echo "dispatched ${WORKFLOW} on ${REF} @ $(date -u +%Y-%m-%dT%H:%M:%SZ)"
