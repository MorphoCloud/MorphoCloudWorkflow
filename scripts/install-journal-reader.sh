#!/usr/bin/env bash
# Give the incident harvester read-only access to one MorphoCloud server VM's
# journal (INCIDENT_STORE_PHASE1_DESIGN.md, "Coverage amendment", class I).
#
# RUN FROM THE MAINTAINER'S MACHINE (this MWF checkout). On the target host it:
#   - installs scripts/mc-journal-read.py as root-owned
#     /usr/local/lib/morphocloud/mc-journal-read.py;
#   - creates the locked system user `mcjournal` (groups systemd-journal and
#     adm, so it can read the journal and /var/log), whose ONLY way in is the
#     acquire runner's key with `restrict,command=` set to the reader: it can
#     run nothing else, forward nothing, get no terminal;
#   - writes /etc/morphocloud/mc-journal-files, the allowlist of our services'
#     log files outside the journal (the host's `files:` in incident-sources.yml);
#   - from the runner, checks the account answers (and records the host key in
#     the runner's known_hosts, which the harvester's BatchMode SSH needs).
#
# Usage:
#   install-journal-reader.sh <admin user@host> <runner user@host> <address the runner uses> [name=/abs/path ...]
#   e.g. install-journal-reader.sh exouser@149.165.151.191 exouser@149.165.152.104 10.0.16.245 \
#          send-welcomes=/var/log/morphocloud-send-welcomes.log js2-status=/var/log/morphocloud-intake-js2-cron.log
# The acquire runner itself needs no account: install-harvester.sh puts the
# reader beside the harvester and adds exouser to systemd-journal.
# Environment: MWF_SSH_JUMP=<user@host> to reach the admin host through a jump
# host (the Test portal and test join VMs: MWF_SSH_JUMP=exouser@149.165.154.162).
set -euo pipefail

ADMIN="${1:?usage: install-journal-reader.sh <admin user@host> <runner user@host> <address> [name=/path ...]}"
RUNNER="${2:?runner user@host}"
ADDR="${3:?the address the runner uses for this host}"
shift 3
JUMP=()
[[ -n "${MWF_SSH_JUMP:-}" ]] && JUMP=(-J "$MWF_SSH_JUMP")
MWF="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
READER="$MWF/scripts/mc-journal-read.py"
[[ -s "$READER" ]] || { echo "ERROR: $READER missing"; exit 1; }

ALLOW=""
for kv in "$@"; do
  name="${kv%%=*}"; path="${kv#*=}"
  [[ "$name" =~ ^[A-Za-z0-9_-]{1,64}$ && "$path" == /* ]] || { echo "ERROR: bad file entry '$kv'"; exit 1; }
  ALLOW+="$name $path"$'\n'
done

PUB="$(ssh -o BatchMode=yes "$RUNNER" 'cat ~/.ssh/id_ed25519.pub')"
[[ "$PUB" == ssh-ed25519\ * ]] || { echo "ERROR: no ed25519 public key on $RUNNER"; exit 1; }
KEYLINE="restrict,command=\"/usr/bin/python3 /usr/local/lib/morphocloud/mc-journal-read.py\" $PUB"

echo ">> Installing the journal reader on $ADMIN (runner $RUNNER reaches it at $ADDR)"
scp -q "${JUMP[@]}" "$READER" "$ADMIN:/tmp/mc-journal-read.py"
# shellcheck disable=SC2029  # KEYLINE and ALLOW are expanded here on purpose
ssh "${JUMP[@]}" "$ADMIN" "
  set -e
  sudo -n install -d -m 755 /usr/local/lib/morphocloud /etc/morphocloud
  sudo -n install -m 755 -o root -g root /tmp/mc-journal-read.py /usr/local/lib/morphocloud/mc-journal-read.py
  rm -f /tmp/mc-journal-read.py
  if ! id mcjournal >/dev/null 2>&1; then
    sudo -n useradd --system --create-home --home-dir /var/lib/mcjournal --shell /bin/sh mcjournal
  fi
  sudo -n usermod -aG systemd-journal,adm mcjournal
  sudo -n passwd -l mcjournal >/dev/null
  sudo -n install -d -m 700 -o mcjournal -g mcjournal /var/lib/mcjournal/.ssh
  printf '%s\n' '$KEYLINE' | sudo -n tee /var/lib/mcjournal/.ssh/authorized_keys >/dev/null
  sudo -n chown mcjournal:mcjournal /var/lib/mcjournal/.ssh/authorized_keys
  sudo -n chmod 600 /var/lib/mcjournal/.ssh/authorized_keys
  printf '%s' '$ALLOW' | sudo -n tee /etc/morphocloud/mc-journal-files >/dev/null
  sudo -n chmod 644 /etc/morphocloud/mc-journal-files
  if sudo -n grep -qsE '^\s*AllowUsers' /etc/ssh/sshd_config /etc/ssh/sshd_config.d/*.conf; then
    echo '  WARNING: sshd has AllowUsers; add mcjournal there or the runner cannot log in'
  fi
  echo \"  mcjournal: \$(id mcjournal)\"
  echo \"  allowlist: \$(wc -l < /etc/morphocloud/mc-journal-files) file(s)\"
"

echo ">> Checking from the runner (a refused request proves the key and the forced command) ..."
# shellcheck disable=SC2029
OUT="$(ssh -o BatchMode=yes "$RUNNER" "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 mcjournal@$ADDR hello 2>&1; echo exit=\$?")"
echo "$OUT" | sed 's/^/  /'
if grep -q "mc-journal-read: refused" <<<"$OUT"; then
  echo ">> OK: the runner reaches mcjournal@$ADDR and only the reader runs there."
else
  echo ">> FAILED: the runner could not run the reader on $ADDR (see above)."
  exit 1
fi
