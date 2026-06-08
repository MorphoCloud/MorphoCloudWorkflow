#!/usr/bin/env bash
# Base host setup for a MorphoCloud/Instances runner VM. RUN ON THE RUNNER.
#
# Installs jq, the GitHub CLI, the docker group, and the OpenStack venv. It does NOT
# register the GitHub runner, place clouds.yaml, the SSH key, or the dispatcher
# tokens/crontab — those are credential/interactive steps in INSTANCES_RUNNER_REBUILD.md.
# Idempotent. Scope: Instances runners only (not MC-* course runners).
set -euo pipefail

echo ">> apt: jq"
sudo apt-get update -y
sudo apt-get install -y jq wget

if ! command -v gh >/dev/null 2>&1; then
  echo ">> installing GitHub CLI"
  sudo mkdir -p -m 755 /etc/apt/keyrings
  wget -nv -O- https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    | sudo tee /etc/apt/keyrings/githubcli-archive-keyring.gpg >/dev/null
  sudo chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    | sudo tee /etc/apt/sources.list.d/github-cli.list >/dev/null
  sudo apt-get update -y && sudo apt-get install -y gh
fi

echo ">> docker group"
sudo groupadd -f docker
sudo usermod -aG docker "$USER"   # log out/in for it to take effect

echo ">> OpenStack venv (~/venv)"
[[ -d ~/venv ]] || python3 -m venv ~/venv
~/venv/bin/python -m pip install --quiet --upgrade pip python-openstackclient

cat <<'NEXT'
>> Base host done. Still required (see INSTANCES_RUNNER_REBUILD.md):
   - ~/.config/openstack/clouds.yaml  (Exosphere -> allocation -> Credentials -> clouds.yaml)
   - ~/.ssh/id_ed25519                (murat-key, for instance SSH)
   - register the GitHub runner       (label: acquire OR control)
   - ACQUIRE runner only: restore-instances-dispatcher.sh  (scripts + tokens + crontab)
NEXT
