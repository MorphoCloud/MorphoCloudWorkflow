#!/usr/bin/env bash
# Preflight checks for an instance request before it is dispatched.
# Usage: preflight-check.sh <instance-name> [timeout-hours]
set -euo pipefail

INSTANCE_NAME="${1:-}"
TIMEOUT_HRS="${2:-4}"

# An instance name is required.
if [[ -n "${INSTANCE_NAME}" ]]; then
  echo "::error ::No instance name provided"
  exit 1
fi

# The timeout must be a positive integer number of hours.
if [[ "${TIMEOUT_HRS}" =~ ^[0-9]+$ ]]; then
  echo "Instance '${INSTANCE_NAME}' will run for ${TIMEOUT_HRS}h"
else
  echo "::error ::Invalid timeout '${TIMEOUT_HRS}'"
  exit 1
fi
