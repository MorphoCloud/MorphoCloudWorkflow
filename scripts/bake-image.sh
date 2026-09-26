#!/bin/bash
# Bake a MorphoCloud golden image (STORAGE_REDESIGN_PLAN.md, section 4).
#
# Boots the stock image with the same cloud-config as /create in share mode,
# waits for setup to finish, removes everything specific to the build instance,
# snapshots it as an image and deletes the build instance. Instances created
# from the image still run ansible at first boot (fast: Slicer is already in
# /opt/slicer, owned by exouser).
#
# Run by an admin on a runner host that has the OpenStack credentials, the
# venv with the openstack CLI, and ~/.ssh/id_ed25519 (the runner key):
#
#   OS_CLOUD=BIO240357_IU scripts/bake-image.sh g3.large morphocloud-share-vgpu-20260926
#   OS_CLOUD=BIO240357_IU scripts/bake-image.sh g3.xl    morphocloud-share-regular-20260926
#
# Bake the vGPU image on a vGPU flavor (g3.large) and the regular-driver image
# on a passthrough flavor (g3.xl or g4.xl): the NVIDIA driver installed during
# setup follows the GPU the build instance sees.
#
# On failure the build instance is kept for diagnosis and its name printed;
# delete it by hand afterwards.
set -euo pipefail

FLAVOR="${1:?flavor, e.g. g3.large}"
IMAGE="${2:?image name, e.g. morphocloud-share-vgpu-20260926}"
CLOUD_CONFIG="${3:-$(cd "$(dirname "$0")/.." && pwd)/cloud-config}"
[[ "$FLAVOR" =~ ^[a-z0-9]+\.[a-z0-9]+$ ]] || { echo "bad flavor: $FLAVOR" >&2; exit 2; }
[[ "$IMAGE" =~ ^[a-z0-9][a-z0-9._-]{2,80}$ ]] || { echo "bad image name: $IMAGE" >&2; exit 2; }
[[ -s "$CLOUD_CONFIG" ]] || { echo "cloud-config not found: $CLOUD_CONFIG" >&2; exit 2; }
[[ -n "${OS_CLOUD:-}" ]] || { echo "set OS_CLOUD" >&2; exit 2; }
[[ -s ~/.ssh/id_ed25519.pub ]] || { echo "runner key ~/.ssh/id_ed25519.pub missing" >&2; exit 2; }

source ~/venv/bin/activate
if [[ -n "$(openstack image list --name "$IMAGE" -f value -c ID)" ]]; then
  echo "an image named $IMAGE already exists; choose another name" >&2
  exit 2
fi

NAME="mc-bake-$(date -u +%Y%m%d-%H%M%S)"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
log() { echo "$(date -u +%H:%M:%S) $*"; }
ssh_opts=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null
          -o ConnectTimeout=10 -o LogLevel=ERROR)

# The same cloud-config /create uses in share mode.
cp "$CLOUD_CONFIG" "$WORK/cloud-config"
sed -i "s|{runner-ssh-public-key}|$(cat ~/.ssh/id_ed25519.pub)|g" "$WORK/cloud-config"
sed -i "s|{session-timeout-hrs}|4|g" "$WORK/cloud-config"
sed -i 's|\\"guac_enabled\\":true,|\\"guac_enabled\\":true,\\"storage_mode\\":\\"share\\",\\"slicer_install_dir\\":\\"/opt/slicer\\",\\"slicer_default_scene_path\\":\\"/home/exouser/Documents\\",|' "$WORK/cloud-config"
grep -q 'storage_mode' "$WORK/cloud-config" || { echo "could not add the share-mode variables" >&2; exit 1; }

log "creating build instance $NAME ($FLAVOR)"
openstack server create "$NAME" \
  --nic net-id="auto_allocated_network" \
  --security-group "exosphere" \
  --flavor "$FLAVOR" \
  --image "Featured-Ubuntu24" \
  --property "exoSetup={\"status\":\"waiting\",\"epoch\":null}" \
  --user-data "$WORK/cloud-config" \
  --wait -f value -c id >/dev/null
fail() { echo "BAKE FAILED: $*" >&2; echo "Build instance kept for diagnosis: $NAME" >&2; exit 1; }

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

log "checking Slicer and cleaning the instance ($IP)"
ssh "${ssh_opts[@]}" exouser@"$IP" 'sudo bash -s' <<'EOF' || fail "clean-up failed"
set -euo pipefail
# Slicer must run and take extensions as exouser, without sudo.
[ -x /opt/slicer/Slicer/Slicer ] || { echo "Slicer is not in /opt/slicer" >&2; exit 1; }
sudo -u exouser test -w /opt/slicer/Slicer || { echo "exouser cannot write /opt/slicer/Slicer" >&2; exit 1; }
[ "$(stat -c %U /opt/slicer)" = exouser ] || { echo "/opt/slicer is not owned by exouser" >&2; exit 1; }
[ -x /usr/local/bin/morphocloud-slicer ] || { echo "launcher missing" >&2; exit 1; }

# Enabled in an image, vncserver@1 starts before docker0 exists, exhausts its
# restart limit and aborts the next ansible run.
systemctl disable vncserver@1 || true
systemctl stop vncserver@1 || true
# The Guacamole containers and configs carry the build instance's passphrase.
docker ps -aq | xargs -r docker rm -f
rm -f /opt/guacamole/config/user-mapping.xml /opt/dropzone/dropzone.conf
# Per-instance identity and secrets; every new instance makes its own.
usermod -p '!' exouser
rm -f /home/exouser/.vnc/passwd /home/exouser/.vnc/*.log /home/exouser/.vnc/*.pid
rm -f /etc/ssh/ssh_host_*
rm -f /home/*/.ssh/authorized_keys /root/.ssh/authorized_keys
truncate -s 0 /etc/machine-id
rm -f /var/lib/dbus/machine-id
rm -f /opt/instance-config-support/slicer_failed_extensions
rm -rf /home/exouser/.cache/sessions /tmp/* /var/tmp/*
# Without this the per-instance guard stays satisfied and setup never runs
# on instances created from the image.
cloud-init clean --logs --seed
sync
EOF

log "shutting down"
openstack server stop "$NAME"
for _ in $(seq 1 60); do
  [[ "$(openstack server show "$NAME" -f value -c status)" == "SHUTOFF" ]] && break
  sleep 5
done
[[ "$(openstack server show "$NAME" -f value -c status)" == "SHUTOFF" ]] || fail "did not shut down"

log "creating image $IMAGE"
openstack server image create --name "$IMAGE" --wait "$NAME" >/dev/null
openstack image set \
  --property mc_baked_on_flavor="$FLAVOR" \
  --property mc_baked_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  "$IMAGE"
fmt=$(openstack image show "$IMAGE" -f value -c disk_format)
[[ "$fmt" == "raw" ]] || echo "WARNING: disk_format is $fmt, not raw (boots will copy instead of clone)" >&2

log "deleting build instance"
openstack server delete --wait "$NAME"
log "done: $IMAGE ($fmt)"
echo "Set it on the repository: MORPHOCLOUD_IMAGE_VGPU (vGPU flavors) or MORPHOCLOUD_IMAGE_REGULAR (all others)."
