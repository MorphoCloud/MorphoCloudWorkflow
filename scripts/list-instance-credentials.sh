#!/usr/bin/env bash

set -euo pipefail

usage() {
  cat <<EOF
Usage: $0 [-p prefix]

  -p prefix   Filter instances by name prefix (default: 'instance')
  -h          Show this help message
EOF
  exit 1
}

INSTANCE_NAME_PREFIX="instance"
while getopts ":p:h" opt; do
  case ${opt} in
    p) INSTANCE_NAME_PREFIX="${OPTARG}" ;;
    h) usage ;;
    \?) echo "Error: Invalid option -${OPTARG}" >&2; usage ;;
    :) echo "Error: Option -${OPTARG} requires an argument." >&2; usage ;;
  esac
done
shift $((OPTIND -1))

# Attempt to auto-activate the Python environment
if ! command -v "openstack" &>/dev/null; then
  . venv/bin/activate
fi

# Prerequisites
for cmd in openstack jq ssh curl; do
  if ! command -v "$cmd" &>/dev/null; then
    echo "Error: required command '$cmd' not found in PATH." >&2
    exit 1
  fi
done

# Attempt to auto‑set OS_CLOUD if not already provided
if [[ -z "${OS_CLOUD:-}" ]]; then
  config="$HOME/.config/openstack/clouds.yaml"
  if [[ -r "$config" ]]; then
    # extract the first top‑level key under "clouds:"
    OS_CLOUD=$(grep -oP '(?<=^  )[A-Za-z0-9_]+(?=:)' "$config" | head -n1)
    export OS_CLOUD
  fi
fi

: "${OS_CLOUD:?Environment variable OS_CLOUD must be set}"

# Header
echo "instance name,ssh,web connect,passphrase"

openstack server list --name "^$INSTANCE_NAME_PREFIX" --status ACTIVE -f json -c "Name" -c "Status" -c "Networks" | \
  jq -c '.[]' | while read -r instance_json; do

    INSTANCE_NAME=$(echo "$instance_json" | jq -r '.Name')
    INSTANCE_IP=$(echo "$instance_json" | jq -r '.Networks.auto_allocated_network[1]')

    # See hard-coded value in exosphere/src/Helpers/Interaction.elm
    guacamole_port=49528

    # See cloud_configs.js (allocation region is "IU")
    proxy_hostname=proxy-js2-iu.exosphere.app

    # See "buildProxyUrl" in src/Helpers/Url.elm
    proxified_instance_ip=${INSTANCE_IP//./-}

    # See hard-coded value in exosphere/src/Helpers/Interaction.elm
    client_id=ZGVza3RvcABjAGRlZmF1bHQ

    connection_url="https://http-$proxified_instance_ip-$guacamole_port.$proxy_hostname/guacamole/#/client/$client_id="

    # Get instance password from tags
    instance_pwd=$(
      openstack server show "$INSTANCE_NAME" -c tags -f json | \
      jq -r '.tags[] | select(startswith("exoPw")) | sub("^exoPw:"; "")'
    )

    # Since 'exoPw' tag may not yet set, attempt to directly retrieve the password using
    # the openstack endpoint local to the instance.
    if [[ -z "$instance_pwd" ]]; then
      function retrieve_password {
        instance_pwd=$(ssh \
          -o StrictHostKeyChecking=no \
          -o UserKnownHostsFile=/dev/null \
          -o LogLevel=ERROR \
          exouser@$instance_ip \
          'curl --silent http://169.254.169.254/openstack/latest/password')
        echo "$instance_pwd"
      }
      echo "Password not found in tags, attempting to retrieve via SSH..." >&2
      for attempt in {1..2}; do
        instance_pwd=$(retrieve_password)
        if [[ -n "$instance_pwd" ]]; then
          break
        fi
        echo "Retrying password retrieval ($attempt)..." >&2
        sleep 20
      done
    fi

    if [[ -z "$instance_pwd" ]]; then
      echo "Error: Failed to retrieve password for $INSTANCE_NAME" >&2
    fi

    echo "$INSTANCE_NAME,exouser@$INSTANCE_IP,$connection_url,$instance_pwd"
done
