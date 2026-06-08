#!/usr/bin/env bash
# Restore the dispatcher "clock" onto a rebuilt MorphoCloud/Instances ACQUIRE runner
# (runner-1): the dispatch scripts, the two PAT token files, and the crontab.
#
# RUN FROM THE MAINTAINER'S MACHINE (needs this MWF checkout + the PAT backups in
# ~/.ssh). The base host (venv, gh, docker, clouds.yaml, runner registration) must
# already be done — see INSTANCES_RUNNER_REBUILD.md.
#
# ⚠️ ACQUIRE RUNNER ONLY. Never run this against the control runner — a crontab there
#    double-fires every scheduled workflow.
#
# Usage: restore-instances-dispatcher.sh [user@host]   (default: exouser@149.165.152.104)
set -euo pipefail

RUNNER="${1:-exouser@149.165.152.104}"
MWF="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WF_TOKEN="$HOME/.ssh/GH_MorphoCloud-workflow-dispatcher"      # -> dispatch.pat
AN_TOKEN="$HOME/.ssh/GH_MorphoCloud-analytics-dispatcher"     # -> analytics-dispatch.pat

echo ">> Target ACQUIRE runner: $RUNNER"
read -r -p "   Confirm this is the ACQUIRE runner (not control) [yes/no]: " ok
[[ "$ok" == "yes" ]] || { echo "Aborted."; exit 1; }

for f in "$MWF/scripts/morphocloud-dispatch.sh" "$MWF/scripts/check-dispatch-tokens.sh" \
         "$MWF/scripts/instances-runner.crontab" "$WF_TOKEN" "$AN_TOKEN"; do
  [[ -s "$f" ]] || { echo "ERROR: required file missing: $f"; exit 1; }
done

echo ">> Copying scripts, tokens, and crontab (values not printed) ..."
ssh "$RUNNER" 'mkdir -p ~/.config/morphocloud && chmod 700 ~/.config/morphocloud'
scp "$MWF/scripts/morphocloud-dispatch.sh"  "$RUNNER:morphocloud-dispatch.sh"
scp "$MWF/scripts/check-dispatch-tokens.sh" "$RUNNER:check-dispatch-tokens.sh"
scp "$MWF/scripts/instances-runner.crontab" "$RUNNER:instances-runner.crontab"
scp "$WF_TOKEN" "$RUNNER:.config/morphocloud/dispatch.pat"
scp "$AN_TOKEN" "$RUNNER:.config/morphocloud/analytics-dispatch.pat"

echo ">> Setting permissions and installing crontab ..."
ssh "$RUNNER" '
  set -e
  chmod +x ~/morphocloud-dispatch.sh ~/check-dispatch-tokens.sh
  chmod 600 ~/.config/morphocloud/dispatch.pat ~/.config/morphocloud/analytics-dispatch.pat
  crontab ~/instances-runner.crontab
  echo "crontab lines: $(crontab -l | grep -cE "morphocloud-dispatch|check-dispatch")"
'

echo ">> Verifying tokens authenticate (expiry header) ..."
ssh "$RUNNER" '
  for f in dispatch.pat analytics-dispatch.pat; do
    t=$(tr -d "\r\n" < ~/.config/morphocloud/$f)
    exp=$(GH_TOKEN="$t" gh api -i /rate_limit 2>/dev/null | grep -i token-expiration | sed "s/.*: //" | tr -d "\r")
    echo "  $f -> ${exp:-FAILED (token invalid or gh missing)}"
  done'

echo ">> Done. Watch ~/morphocloud-dispatch.log on the runner for 'dispatched' lines."
