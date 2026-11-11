#!/usr/bin/env bash
# MorphoCloud workflow dispatcher.
#
# GitHub's `on: schedule` cron is best-effort and heavily throttled — a `*/5`
# workflow was observed firing only every ~4-5 hours, so instances shelved hours
# late. A crontab on the Instances self-hosted runner VM runs this to trigger the
# time-sensitive workflows on time via `workflow_dispatch` (honored immediately).
# The workflows keep their `on: schedule` as a loose backstop.
#
# Scope: MorphoCloud/Instances and Test-Instances (MC-* course repos stay on
# GitHub's schedule by design). The crontab lines are rendered from the
# workflows' '# dispatch:' markers by scripts/check-workflow-clock.py.
#
# Setup + crontab: see scripts/runner-cron-dispatcher.md
# Usage: MORPHOCLOUD_DISPATCH_REPO=<owner/repo> morphocloud-dispatch.sh <workflow1.yml> [<workflow2.yml> ...]
set -u
# REPO defaults to Instances (the lifecycle crons); override via env for other repos
# (e.g. MORPHOCLOUD_DISPATCH_REPO=MorphoCloud/MorphoCloudAnalytics for the usage report,
# paired with MORPHOCLOUD_DISPATCH_TOKEN_FILE pointing at that repo's dispatch token).
REPO="${MORPHOCLOUD_DISPATCH_REPO:-MorphoCloud/Instances}"
REF="main" # explicit ref avoids a default-branch lookup that would need Contents:read
TOKEN_FILE="${MORPHOCLOUD_DISPATCH_TOKEN_FILE:-$HOME/.config/morphocloud/dispatch.pat}"
LOG="$HOME/morphocloud-dispatch.log"
ts="$(date -u +%FT%TZ)"

if [[ ! -s "$TOKEN_FILE" ]]; then
  echo "$ts ERROR token file missing/empty ($TOKEN_FILE) — skipping [$*]" >>"$LOG"
  exit 0
fi
GH_TOKEN="$(tr -d '\r\n' <"$TOKEN_FILE")"
export GH_TOKEN

# One uniform line per dispatch, written the moment gh returns, so the incident
# harvester can tell this clock's runs from a maintainer's hand dispatch
# (INCIDENT_STORE_PLAN.md, actor_kind rule 1; scripts/check-dispatch-logging.py):
#   <UTC> DISPATCH <wf> repo=<owner/repo> issue=- by=morphocloud-dispatch.sh result=ok|FAILED <stderr tail>
for wf in "$@"; do
  if err=$(gh workflow run "$wf" --repo "$REPO" --ref "$REF" 2>&1 >/dev/null); then
    echo "$ts DISPATCH $wf repo=$REPO issue=- by=morphocloud-dispatch.sh result=ok" >>"$LOG"
  else
    err=$(printf '%s' "$err" | tr '\n' ' ')
    # (bash's "${err: -200}" is EMPTY when err is shorter than 200 characters)
    if (( ${#err} > 200 )); then err="${err: -200}"; fi
    echo "$ts DISPATCH $wf repo=$REPO issue=- by=morphocloud-dispatch.sh result=FAILED $err" >>"$LOG"
  fi
done
