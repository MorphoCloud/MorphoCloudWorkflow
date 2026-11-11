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

for f in "$MWF/scripts/harvest-incidents.py" "$MWF/scripts/mc-journal-read.py" "$MWF/scripts/openstack-ro" "$MWF/incident-sources.yml" \
         "$MWF/incident-signatures.yml" "$MWF/.github/actions/record-facts/redact.sed" \
         "$SRC_TOKEN" "$STORE_TOKEN"; do
  [[ -s "$f" ]] || { echo "ERROR: required file missing: $f"; exit 1; }
done

echo ">> Installing the incident harvester on $RUNNER"
echo "   source $SOURCE_REPO -> store $STORE_REPO"
ssh "$RUNNER" 'mkdir -p ~/.config/morphocloud && chmod 700 ~/.config/morphocloud'
scp -q "$MWF/scripts/harvest-incidents.py" "$RUNNER:harvest-incidents.py"
scp -q "$MWF/scripts/mc-journal-read.py" "$RUNNER:mc-journal-read.py"
scp -q "$MWF/incident-sources.yml" "$MWF/incident-signatures.yml" "$RUNNER:"
scp -q "$MWF/.github/actions/record-facts/redact.sed" "$RUNNER:redact.sed"
scp -q "$MWF/scripts/openstack-ro" "$RUNNER:venv/bin/openstack-ro"
scp -q "$SRC_TOKEN" "$RUNNER:.config/morphocloud/harvest-source.pat"
scp -q "$STORE_TOKEN" "$RUNNER:.config/morphocloud/harvest-store.pat"

echo ">> Store checkout, credential helper, identity (the token never enters .git/config) ..."
ssh "$RUNNER" "
  set -e
  chmod +x ~/harvest-incidents.py ~/mc-journal-read.py ~/venv/bin/openstack-ro
  # class I: the runner reads its own system journal (cron picks the group up
  # at its next job); other hosts are read through install-journal-reader.sh
  # Without it the reader refuses and every cycle records this runner as
  # unreachable; stop the install here, loudly, rather than install that.
  if ! id -nG | grep -qw systemd-journal; then
    sudo -n usermod -aG systemd-journal \$(whoami) || {
      echo 'ERROR: cannot add this account to systemd-journal (needs passwordless sudo); install stopped'
      exit 1
    }
  fi
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

# Class K: the source token must read the source and every repository the
# source lists under extra_repos, and nothing tells us otherwise at run time
# but a 404; check each one now.
echo ">> Repositories the source token must read (source + extra_repos) ..."
ssh "$RUNNER" "MWF_SOURCE_REPO=$SOURCE_REPO ~/venv/bin/python - <<'PY'
import json, os, urllib.error, urllib.request, yaml
src = os.environ['MWF_SOURCE_REPO']
cfg = yaml.safe_load(open(os.path.expanduser('~/incident-sources.yml')))
repos = [src] + list(cfg['sources'][src].get('extra_repos') or [])
tok = open(os.path.expanduser('~/.config/morphocloud/harvest-source.pat')).read().strip()
bad = 0
for r in repos:
    req = urllib.request.Request('https://api.github.com/repos/' + r + '/actions/runs?per_page=1',
                                 headers={'Authorization': 'Bearer ' + tok, 'User-Agent': 'install-harvester'})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            print('  OK   ', r, '(', json.load(resp).get('total_count'), 'runs )')
    except urllib.error.HTTPError as e:
        bad += 1
        print('  FAIL ', r, 'HTTP', e.code, '- add it to the token (Only select repositories)')
raise SystemExit(1 if bad else 0)
PY" || echo "  WARNING: the harvester will name these repositories in repos_unreadable until the token is fixed"

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
