#!/usr/bin/env bash
# Install the incident harvester onto a MorphoCloud ACQUIRE runner: the script,
# its three data files, the read-only OpenStack wrapper, the two PAT files, and
# the store checkout. INCIDENT_STORE_PHASE1_DESIGN.md, "Renderer and restore".
#
# RUN FROM THE MAINTAINER'S MACHINE (this MWF checkout + the PAT backups in
# ~/.ssh). Two callers:
#   - restore-instances-dispatcher.sh (from the 2026-11-23 merge): files and
#     tokens only; the crontab line is inside the rendered managed block.
#   - by hand with --outside-block (production BEFORE the merge, whose crontab
#     is not rendered from share-prototype): also appends the crontab line
#     after the managed block when no harvest-incidents line exists yet.
#
# Usage: install-harvester.sh <user@host> [--outside-block]
#
# Environment (defaults are production):
#   MWF_HARVEST_SOURCE_TOKEN  PAT file: Actions:read + Issues:read on the source repo
#                             (default ~/.ssh/GH_MorphoCloud-incidents-source;
#                             Test: ~/.ssh/GH_MorphoCloud-test-incidents-source)
#   MWF_HARVEST_STORE_TOKEN   PAT file: Contents:write + Issues:write on the store
#                             (default ~/.ssh/GH_MorphoCloud-incidents-store;
#                             Test: ~/.ssh/GH_MorphoCloud-test-incidents-store)
#   MWF_SOURCE_REPO           default MorphoCloud/Instances (Test: MorphoCloud/Test-Instances)
#   MWF_STORE_REPO            default MorphoCloud/Incidents (Test: MorphoCloud/Incidents-sandbox)
#   MWF_OS_CLOUD              default BIO180006_IU (Test: BIO240357_IU); --outside-block only
set -euo pipefail

RUNNER="${1:?usage: install-harvester.sh <user@host> [--outside-block]}"
OUTSIDE_BLOCK="${2:-}"
MWF="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC_TOKEN="${MWF_HARVEST_SOURCE_TOKEN:-$HOME/.ssh/GH_MorphoCloud-incidents-source}"
STORE_TOKEN="${MWF_HARVEST_STORE_TOKEN:-$HOME/.ssh/GH_MorphoCloud-incidents-store}"
SOURCE_REPO="${MWF_SOURCE_REPO:-MorphoCloud/Instances}"
STORE_REPO="${MWF_STORE_REPO:-MorphoCloud/Incidents}"
OS_CLOUD_NAME="${MWF_OS_CLOUD:-BIO180006_IU}"

for f in "$MWF/scripts/harvest-incidents.py" "$MWF/scripts/openstack-ro" "$MWF/incident-sources.yml" \
         "$MWF/incident-signatures.yml" "$MWF/.github/actions/record-facts/redact.sed" \
         "$SRC_TOKEN" "$STORE_TOKEN"; do
  [[ -s "$f" ]] || { echo "ERROR: required file missing: $f"; exit 1; }
done

echo ">> Installing the incident harvester on $RUNNER"
echo "   source $SOURCE_REPO -> store $STORE_REPO"
ssh "$RUNNER" 'mkdir -p ~/.config/morphocloud && chmod 700 ~/.config/morphocloud'
scp -q "$MWF/scripts/harvest-incidents.py" "$RUNNER:harvest-incidents.py"
scp -q "$MWF/incident-sources.yml" "$MWF/incident-signatures.yml" "$RUNNER:"
scp -q "$MWF/.github/actions/record-facts/redact.sed" "$RUNNER:redact.sed"
scp -q "$MWF/scripts/openstack-ro" "$RUNNER:venv/bin/openstack-ro"
scp -q "$SRC_TOKEN" "$RUNNER:.config/morphocloud/harvest-source.pat"
scp -q "$STORE_TOKEN" "$RUNNER:.config/morphocloud/harvest-store.pat"

echo ">> Store checkout, credential helper, identity (the token never enters .git/config) ..."
ssh "$RUNNER" "
  set -e
  chmod +x ~/harvest-incidents.py ~/venv/bin/openstack-ro
  chmod 600 ~/.config/morphocloud/*.pat
  if [[ ! -d ~/incidents-store/.git ]]; then
    git -c credential.helper='!f() { test \"\$1\" = get && printf \"username=x-access-token\npassword=%s\n\" \"\$(tr -d \"\\r\\n\" < ~/.config/morphocloud/harvest-store.pat)\"; }; f' \
        clone -q https://github.com/$STORE_REPO.git ~/incidents-store
  fi
  cd ~/incidents-store
  git config credential.helper '!f() { test \"\$1\" = get && printf \"username=x-access-token\npassword=%s\n\" \"\$(tr -d \"\\r\\n\" < ~/.config/morphocloud/harvest-store.pat)\"; }; f'
  git config user.name 'MorphoCloud harvester'
  git config user.email 'no-reply@morphocloud.org'
  git config pull.rebase true
  echo \"  store at ~/incidents-store: \$(git rev-parse --short HEAD) on \$(git branch --show-current)\"
"

echo ">> Verifying the tokens authenticate (expiry header) ..."
ssh "$RUNNER" '
  for f in harvest-source harvest-store; do
    t=$(tr -d "\r\n" < ~/.config/morphocloud/$f.pat)
    exp=$(GH_TOKEN="$t" gh api -i /rate_limit 2>/dev/null | grep -i token-expiration | sed "s/.*: //" | tr -d "\r")
    echo "  $f.pat -> ${exp:-FAILED (token invalid or gh missing)}"
  done'

if [[ "$OUTSIDE_BLOCK" == "--outside-block" ]]; then
  # The same line check-workflow-clock.py renders into the managed block (its
  # RUNNER_SCRIPTS entry); test_harvest_incidents.py keeps the two equal.
  LINE="*/5 * * * * OS_CLOUD=$OS_CLOUD_NAME MORPHOCLOUD_SOURCE_REPO=$SOURCE_REPO MORPHOCLOUD_STORE_REPO=$STORE_REPO /home/exouser/venv/bin/python /home/exouser/harvest-incidents.py >> /home/exouser/harvest-incidents.log 2>&1"
  echo ">> Crontab line outside the managed block (pre-merge production) ..."
  ssh "$RUNNER" "
    if crontab -l 2>/dev/null | grep -q harvest-incidents; then
      echo '  a harvest-incidents line already exists; not adding another'
    else
      { crontab -l 2>/dev/null; echo '# Incident harvester (hand-installed before the 2026-11-23 merge; the rendered block replaces this line)'; echo \"$LINE\"; } | crontab -
      echo '  added'
    fi
    crontab -l | grep -c harvest-incidents | sed 's/^/  harvest-incidents lines: /'"
else
  echo ">> Files and tokens installed. The crontab line comes from the rendered managed block (restore-instances-dispatcher.sh)."
fi
echo ">> Next: ssh $RUNNER 'MORPHOCLOUD_SOURCE_REPO=$SOURCE_REPO OS_CLOUD=$OS_CLOUD_NAME ~/venv/bin/python ~/harvest-incidents.py --dry-run'"
