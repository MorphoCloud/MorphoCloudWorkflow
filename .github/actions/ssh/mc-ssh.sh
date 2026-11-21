# shellcheck shell=bash
# Time-limited SSH from a runner (SETUP_SSH_BOUNDS_DESIGN.md).
#
# Source this file, then call:
#
#   mc_ssh LIMIT_SECONDS LABEL [ssh options and arguments...]
#
# ConnectTimeout limits only how long the connection takes to open. Once it
# is open, an SSH call can wait forever in two ways:
#
#   1. A process started on the instance keeps the session's output open
#      (Test-Instances #575: a gvfsd-fuse left by `gio` held sshd's pipes).
#   2. The instance stops answering in the middle of the call.
#
# `timeout` bounds the whole call (case 1). ServerAliveInterval and
# ServerAliveCountMax end a call to an instance that stopped answering in
# about 60 s (case 2). OpenSSH keeps the first value given for an option, and
# no caller sets these two, so putting them first is safe.
#
# Exit status: the ssh status, except 124 (or 137 when ssh ignored SIGTERM and
# was killed 10 s later) when the time limit was reached. In that case the
# helper prints an ::error annotation naming LABEL and the limit, on stderr so
# that a caller capturing stdout with $(...) does not swallow it. LABEL is
# printed; the ssh arguments are not, because a remote command can carry
# values that must not appear in the log.
#
# Safe under `set -e`: the status is collected with `||`, so the annotation is
# printed before the caller's shell sees the failure.
mc_ssh() {
  local limit=$1 label=$2 rc=0
  shift 2
  timeout -k 10 "$limit" ssh \
    -o ServerAliveInterval=15 -o ServerAliveCountMax=4 "$@" || rc=$?
  if (( rc == 124 || rc == 137 )); then
    echo "::error ::$label: SSH did not finish within ${limit}s (time limit reached)" >&2
  fi
  return "$rc"
}
