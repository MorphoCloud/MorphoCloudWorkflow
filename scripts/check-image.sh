#!/bin/bash
# Check a freshly baked golden image before anyone uses it
# (STORAGE_REDESIGN_PLAN.md, "Monthly bake", decision 22).
#
# Boots one instance from the image with the cloud-config /create uses for a
# baked image in share mode (package refresh and upgrade off, decision 18),
# waits for setup to finish, then checks on the instance:
#   - setup completed and reported no failed Slicer extension;
#   - nvidia-smi lists a GPU (every flavor the check uses has one);
#   - Slicer starts with no main window and exits by itself;
#   - the Desktop has the Slicer launcher and no Extend Instance Session one.
# The check instance (mc-bakecheck-<time>) is deleted on every exit.
#
#   OS_CLOUD=BIO180006_IU scripts/check-image.sh g3.large morphocloud-share-vgpu-20261124 [cloud-config]
#
# Exit 0: the image passed. Exit 1: it failed (the reason is the last line on
# stderr, "CHECK FAILED: ..."). Exit 3: Jetstream2 gave no host, so nothing was
# checked; the caller may try again.
set -euo pipefail

FLAVOR="${1:?flavor, e.g. g3.large}"
IMAGE="${2:?image name}"
CLOUD_CONFIG="${3:-$(cd "$(dirname "$0")/.." && pwd)/cloud-config}"
[[ "$FLAVOR" =~ ^[a-z0-9]+\.[a-z0-9]+$ ]] || { echo "bad flavor: $FLAVOR" >&2; exit 2; }
[[ "$IMAGE" =~ ^[a-z0-9][a-z0-9._-]{2,80}$ ]] || { echo "bad image name: $IMAGE" >&2; exit 2; }
[[ -s "$CLOUD_CONFIG" ]] || { echo "cloud-config not found: $CLOUD_CONFIG" >&2; exit 2; }
[[ -n "${OS_CLOUD:-}" ]] || { echo "set OS_CLOUD" >&2; exit 2; }
[[ -s ~/.ssh/id_ed25519.pub ]] || { echo "runner key ~/.ssh/id_ed25519.pub missing" >&2; exit 2; }

source ~/venv/bin/activate
[[ -n "$(openstack image list --name "$IMAGE" -f value -c ID)" ]] || { echo "no image named $IMAGE" >&2; exit 2; }

NAME="mc-bakecheck-$(date -u +%Y%m%d-%H%M%S)"
WORK=$(mktemp -d)
cleanup() {
  rm -rf "$WORK"
  if openstack server show "$NAME" -f value -c id >/dev/null 2>&1; then
    openstack server delete --wait "$NAME" >/dev/null 2>&1 || echo "could not delete $NAME; delete it by hand" >&2
  fi
}
trap cleanup EXIT
log() { echo "$(date -u +%H:%M:%S) $*"; }
fail() { echo "CHECK FAILED: $*" >&2; exit 1; }
ssh_opts=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null
          -o ConnectTimeout=10 -o LogLevel=ERROR)

# The cloud-config /create uses for a baked image in share mode.
cp "$CLOUD_CONFIG" "$WORK/cloud-config"
sed -i "s|{runner-ssh-public-key}|$(cat ~/.ssh/id_ed25519.pub)|g" "$WORK/cloud-config"
sed -i "s|{session-timeout-hrs}|4|g" "$WORK/cloud-config"
sed -i 's|\\"guac_enabled\\":true,|\\"guac_enabled\\":true,\\"storage_mode\\":\\"share\\",\\"slicer_install_dir\\":\\"/opt/slicer\\",\\"slicer_default_scene_path\\":\\"/home/exouser/Documents\\",|' "$WORK/cloud-config"
grep -q 'storage_mode' "$WORK/cloud-config" || fail "could not add the share-mode variables"
sed -i 's|\\"dropzone_enabled\\":true|\\"dropzone_enabled\\":false|' "$WORK/cloud-config"
sed -i 's|^package_upgrade: true # {install-os-updates}$|package_upgrade: false # {install-os-updates}|' "$WORK/cloud-config"
grep -q '^package_upgrade: false' "$WORK/cloud-config" || fail "could not turn off package_upgrade"
sed -i 's|^package_update: true$|package_update: false|' "$WORK/cloud-config"
grep -q '^package_update: false$' "$WORK/cloud-config" || fail "could not turn off package_update"

log "creating check instance $NAME ($FLAVOR, $IMAGE)"
if ! openstack server create "$NAME" \
  --nic net-id="auto_allocated_network" \
  --security-group "exosphere" \
  --flavor "$FLAVOR" \
  --image "$IMAGE" \
  --property "exoSetup={\"status\":\"waiting\",\"epoch\":null}" \
  --user-data "$WORK/cloud-config" \
  --wait -f value -c id >/dev/null; then
  fault=$(openstack server show "$NAME" -f json -c fault 2>/dev/null | jq -r '.fault.message // empty' || true)
  if grep -qE "Exhausted all hosts|No valid host" <<<"$fault"; then
    echo "no host for $FLAVOR: $fault" >&2
    exit 3
  fi
  fail "the check instance did not reach ACTIVE: ${fault:-no fault message}"
fi

log "waiting for setup (up to 30 minutes)"
start=$SECONDS
while :; do
  line=$(openstack console log show "$NAME" | grep -E '^\{"status":"' | tail -n1 || true)
  status=pending
  [[ -n "$line" ]] && status=$(jq -r '.status // "pending"' <<<"$line" 2>/dev/null || echo pending)
  [[ "$status" == "complete" ]] && break
  [[ "$status" == "error" ]] && fail "setup reported an error: $line"
  (( SECONDS - start > 1800 )) && fail "setup did not finish in 30 minutes"
  sleep 20
done
failed_ext=$(jq -r '.failed_extensions // empty' <<<"$line")
[[ -z "$failed_ext" ]] || fail "Slicer extensions failed to install: $failed_ext"
log "setup complete after $(( (SECONDS - start) / 60 )) minutes"

IP=$(openstack server show "$NAME" -f json -c addresses \
  | jq -r '.addresses | to_entries[0].value[] | select(test("^10\\."))' | head -1)
[[ -n "$IP" ]] || fail "no private address"

log "checking the GPU, Slicer and the desktop ($IP)"
cat > "$WORK/check.sh" <<'EOF'
set -uo pipefail
nvidia-smi -L | grep -q '^GPU ' || { echo "nvidia-smi lists no GPU"; exit 1; }
# Slicer as the member runs it, on the desktop's display, with no main window;
# it must exit by itself within 5 minutes.
sudo -u exouser env HOME=/home/exouser DISPLAY=:1 \
  timeout 300 /usr/local/bin/morphocloud-slicer --no-splash --no-main-window \
  --python-code 'slicer.app.exit(0)' >/tmp/slicer-check.log 2>&1 \
  || { echo "Slicer did not start and exit cleanly (exit $?): $(tail -n2 /tmp/slicer-check.log | tr '\n' ' ')"; exit 1; }
[ -e /home/exouser/Desktop/Slicer.desktop ] || { echo "no Slicer launcher on the Desktop"; exit 1; }
[ ! -e /home/exouser/Desktop/ExtendInstanceSession.desktop ] || { echo "the Extend Instance Session launcher is on the Desktop"; exit 1; }
echo "all checks passed"
EOF
out=$(ssh "${ssh_opts[@]}" exouser@"$IP" 'sudo bash -s' < "$WORK/check.sh" 2>&1) \
  || fail "$(tail -n1 <<<"$out")"
log "$(tail -n1 <<<"$out")"
log "deleting check instance"
openstack server delete --wait "$NAME"
log "PASSED: $IMAGE on $FLAVOR"
