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
#   MWF_HARVEST_SOURCE_TOKEN, MWF_HARVEST_STORE_TOKEN, MWF_SOURCE_REPO, MWF_STORE_REPO
#                        passed through to scripts/install-harvester.sh (the incident
#                        harvester, INCIDENT_STORE_PLAN.md). A MISSING harvester token
#                        never blocks the restore: a warning is printed, the harvester
#                        line is left out of the installed crontab, everything else
#                        is installed; the store's watchdog then reports it stopped.
set -euo pipefail

RUNNER="${1:-exouser@149.165.152.104}"
MWF="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CRONTAB="${MWF_CRONTAB:-$MWF/scripts/instances-runner.crontab}"
WF_TOKEN="${MWF_WF_TOKEN:-$HOME/.ssh/GH_MorphoCloud-workflow-dispatcher}"      # -> dispatch.pat
AN_TOKEN="${MWF_ANALYTICS_TOKEN-$HOME/.ssh/GH_MorphoCloud-analytics-dispatcher}" # -> analytics-dispatch.pat
HV_SRC_TOKEN="${MWF_HARVEST_SOURCE_TOKEN:-$HOME/.ssh/GH_MorphoCloud-incidents-source}"
HV_STORE_TOKEN="${MWF_HARVEST_STORE_TOKEN:-$HOME/.ssh/GH_MorphoCloud-incidents-store}"

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

# The harvester: files and tokens through its own installer; the crontab line
# is inside the rendered block. Without both tokens the line is stripped from
# the crontab installed below and the clock is restored without it.
HARVEST=1
if [[ ! -s "$HV_SRC_TOKEN" || ! -s "$HV_STORE_TOKEN" ]]; then
  HARVEST=0
  echo "WARNING: incident-harvester token(s) missing ($HV_SRC_TOKEN, $HV_STORE_TOKEN):"
  echo "         installing the clock WITHOUT the harvester line. The store watchdog will"
  echo "         report the harvester as stopped; rerun with the tokens to add it."
fi

echo ">> Copying scripts, tokens, and crontab (values not printed) ..."
ssh "$RUNNER" 'mkdir -p ~/.config/morphocloud && chmod 700 ~/.config/morphocloud'
scp "$MWF/scripts/morphocloud-dispatch.sh" "$RUNNER:morphocloud-dispatch.sh"
scp "$MWF/scripts/police-floating-ips.py" "$RUNNER:police-floating-ips.py"
if (( HARVEST )); then
  scp "$CRONTAB" "$RUNNER:instances-runner.crontab"
else
  grep -v "harvest-incidents" "$CRONTAB" | ssh "$RUNNER" 'cat > ~/instances-runner.crontab'
fi
scp "$WF_TOKEN" "$RUNNER:.config/morphocloud/dispatch.pat"
if [[ -n "$AN_TOKEN" ]]; then
  scp "$MWF/scripts/check-dispatch-tokens.sh" "$RUNNER:check-dispatch-tokens.sh"
  scp "$AN_TOKEN" "$RUNNER:.config/morphocloud/analytics-dispatch.pat"
fi

echo ">> Setting permissions and installing the crontab's managed block ..."
# Only the block between the BEGIN/END markers is ours. Everything else in the
# runner's crontab is kept (the Test runner's portal pickup line lives there — an
# earlier version of this script replaced the whole crontab and silenced it).
# Dispatcher lines outside the block (pre-marker installs) are dropped too, and
# so is a hand-added harvest-incidents line (production before the 2026-11-23
# merge), so it cannot double-fire beside the rendered one.
ssh "$RUNNER" '
  set -e
  chmod +x ~/morphocloud-dispatch.sh
  [[ -f ~/check-dispatch-tokens.sh ]] && chmod +x ~/check-dispatch-tokens.sh
  chmod 600 ~/.config/morphocloud/*.pat
  kept=$(crontab -l 2>/dev/null \
    | sed "/^# BEGIN morphocloud-dispatcher/,/^# END morphocloud-dispatcher/d" \
    | grep -vE "morphocloud-dispatch\.sh|check-dispatch-tokens\.sh|harvest-incidents" || true)
  { [[ -n "$kept" ]] && printf "%s\n" "$kept"; cat ~/instances-runner.crontab; } | crontab -
  echo "kept outside the block: $(printf "%s\n" "$kept" | grep -cvE "^\s*(#|$)") line(s)"
  printf "%s\n" "$kept" | grep -vE "^\s*(#|$)" | cut -c1-100 | sed "s/^/    /"
  echo "dispatcher lines: $(crontab -l | grep -cE "morphocloud-dispatch|check-dispatch")"
  echo "harvester lines: $(crontab -l | grep -c harvest-incidents || true)"
'

if (( HARVEST )); then
  echo ">> Installing the incident harvester (files, tokens, store checkout) ..."
  MWF_HARVEST_SOURCE_TOKEN="$HV_SRC_TOKEN" MWF_HARVEST_STORE_TOKEN="$HV_STORE_TOKEN" \
    "$MWF/scripts/install-harvester.sh" "$RUNNER"
fi

echo ">> Verifying tokens authenticate (expiry header) ..."
ssh "$RUNNER" '
  for f in ~/.config/morphocloud/*.pat; do
    t=$(tr -d "\r\n" < "$f")
    exp=$(GH_TOKEN="$t" gh api -i /rate_limit 2>/dev/null | grep -i token-expiration | sed "s/.*: //" | tr -d "\r")
    echo "  $(basename "$f") -> ${exp:-FAILED (token invalid or gh missing)}"
  done'

echo ">> Done. Watch ~/morphocloud-dispatch.log on the runner for 'DISPATCH ... result=ok' lines."
