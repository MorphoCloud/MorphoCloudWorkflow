#!/usr/bin/env bash
# Install (or restore) the dispatcher "clock" onto a MorphoCloud ACQUIRE runner:
# the dispatch scripts, the PAT token file(s), and the rendered crontab.
#
# RUN FROM THE MAINTAINER'S MACHINE (needs this MWF checkout + the PAT backups in
# ~/.ssh). The base host (venv, gh, docker, clouds.yaml, runner registration) must
# already be done — see INSTANCES_RUNNER_REBUILD.md.
#
# ⚠️ ACQUIRE RUNNER ONLY. Never run this against the control runner — a crontab there
#    double-fires every scheduled workflow.
#
# Usage: restore-instances-dispatcher.sh [user@host]   (default: exouser@149.165.152.104)
#
# Environment (defaults are the production Instances runner):
#   MWF_CRONTAB          crontab to install, rendered by scripts/check-workflow-clock.py
#                        (default scripts/instances-runner.crontab; Test-Instances uses
#                        scripts/test-instances-runner.crontab)
#   MWF_WF_TOKEN         fine-grained PAT, Actions:read/write on the dispatched repo
#                        (default ~/.ssh/GH_MorphoCloud-workflow-dispatcher)
#   MWF_ANALYTICS_TOKEN  PAT for the MorphoCloudAnalytics lines; set to "" to skip
#                        it and check-dispatch-tokens.sh (Test-Instances has neither)
set -euo pipefail

RUNNER="${1:-exouser@149.165.152.104}"
MWF="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CRONTAB="${MWF_CRONTAB:-$MWF/scripts/instances-runner.crontab}"
WF_TOKEN="${MWF_WF_TOKEN:-$HOME/.ssh/GH_MorphoCloud-workflow-dispatcher}"      # -> dispatch.pat
AN_TOKEN="${MWF_ANALYTICS_TOKEN-$HOME/.ssh/GH_MorphoCloud-analytics-dispatcher}" # -> analytics-dispatch.pat

echo ">> Target ACQUIRE runner: $RUNNER"
echo "   crontab: $CRONTAB"
read -r -p "   Confirm this is the ACQUIRE runner (not control) [yes/no]: " ok
[[ "$ok" == "yes" ]] || { echo "Aborted."; exit 1; }

for f in "$MWF/scripts/morphocloud-dispatch.sh" "$CRONTAB" "$WF_TOKEN"; do
  [[ -s "$f" ]] || { echo "ERROR: required file missing: $f"; exit 1; }
done
if [[ -n "$AN_TOKEN" ]]; then
  for f in "$MWF/scripts/check-dispatch-tokens.sh" "$AN_TOKEN"; do
    [[ -s "$f" ]] || { echo "ERROR: required file missing: $f"; exit 1; }
  done
fi
# The crontab must be current with the workflows' '# dispatch:' markers. The
# checker needs PyYAML; pre-commit's own environment has it, a bare python3
# on the maintainer's machine usually does not.
check_failed() { echo "ERROR: scripts/check-workflow-clock.py check failed (see above: stale crontab or unmarked workflow)."; exit 1; }
if command -v pre-commit >/dev/null; then
  (cd "$MWF" && pre-commit run check-workflow-clock --all-files) || check_failed
else
  python3 -c 'import yaml' 2>/dev/null \
    || { echo "ERROR: neither pre-commit nor a python3 with PyYAML is available; install pre-commit (pipx install pre-commit) and retry."; exit 1; }
  python3 "$MWF/scripts/check-workflow-clock.py" check || check_failed
fi

echo ">> Copying scripts, tokens, and crontab (values not printed) ..."
ssh "$RUNNER" 'mkdir -p ~/.config/morphocloud && chmod 700 ~/.config/morphocloud'
scp "$MWF/scripts/morphocloud-dispatch.sh" "$RUNNER:morphocloud-dispatch.sh"
scp "$CRONTAB" "$RUNNER:instances-runner.crontab"
scp "$WF_TOKEN" "$RUNNER:.config/morphocloud/dispatch.pat"
if [[ -n "$AN_TOKEN" ]]; then
  scp "$MWF/scripts/check-dispatch-tokens.sh" "$RUNNER:check-dispatch-tokens.sh"
  scp "$AN_TOKEN" "$RUNNER:.config/morphocloud/analytics-dispatch.pat"
fi

echo ">> Setting permissions and installing crontab ..."
ssh "$RUNNER" '
  set -e
  chmod +x ~/morphocloud-dispatch.sh
  [[ -f ~/check-dispatch-tokens.sh ]] && chmod +x ~/check-dispatch-tokens.sh
  chmod 600 ~/.config/morphocloud/*.pat
  crontab ~/instances-runner.crontab
  echo "crontab lines: $(crontab -l | grep -cE "morphocloud-dispatch|check-dispatch")"
'

echo ">> Verifying tokens authenticate (expiry header) ..."
ssh "$RUNNER" '
  for f in ~/.config/morphocloud/*.pat; do
    t=$(tr -d "\r\n" < "$f")
    exp=$(GH_TOKEN="$t" gh api -i /rate_limit 2>/dev/null | grep -i token-expiration | sed "s/.*: //" | tr -d "\r")
    echo "  $(basename "$f") -> ${exp:-FAILED (token invalid or gh missing)}"
  done'

echo ">> Done. Watch ~/morphocloud-dispatch.log on the runner for 'dispatched' lines."
