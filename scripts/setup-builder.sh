#!/usr/bin/env bash
# One-time setup of the appliance builder service (Administration > Appliance builder).
#
#   sudo bash scripts/setup-builder.sh [--build-user gns3] [--service-user www-data]
#
# The builder runs as the GNS3 user because it needs /dev/kvm and GNS3's QEMU image folder; the portal stays
# unprivileged and only exchanges job files with it in /var/lib/gns3-lab-portal-builder. Safe to re-run.
set -euo pipefail

PORTAL_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
BUILD_USER=gns3
SERVICE_USER=www-data
BUILDER_DIR=/var/lib/gns3-lab-portal-builder
fail() { printf '\nSETUP STOPPED: %s\n' "$*" >&2; exit 1; }
step() { printf '\n==> %s\n' "$*"; }
while [[ $# -gt 0 ]]; do
  case "$1" in
    --build-user) BUILD_USER="${2:-}"; shift 2 ;;
    --service-user) SERVICE_USER="${2:-}"; shift 2 ;;
    *) fail "usage: sudo bash $0 [--build-user gns3] [--service-user www-data]" ;;
  esac
done
[[ $EUID -eq 0 ]] || fail "run as root: sudo bash $0"
id "$BUILD_USER" >/dev/null 2>&1 || fail "user $BUILD_USER does not exist (use --build-user)."
id "$SERVICE_USER" >/dev/null 2>&1 || fail "user $SERVICE_USER does not exist (use --service-user)."
command -v setfacl >/dev/null || fail "setfacl is missing (sudo apt install acl)."

step "Installing QEMU, ISO and SSH tools"
apt-get install -y --no-install-recommends qemu-system-x86 qemu-utils xorriso openssh-client python3 acl
getent group kvm >/dev/null || fail "no kvm group: this server has no /dev/kvm (is virtualization enabled?)."
[[ -e /dev/kvm ]] || fail "/dev/kvm is missing: enable virtualization (and nested virtualization if this is a VM)."

step "Creating $BUILDER_DIR"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0755 "$BUILDER_DIR" "$BUILDER_DIR/jobs" "$BUILDER_DIR/recipes"   # recipes: imported in the portal, read by the builder
install -d -o "$BUILD_USER" -g "$BUILD_USER" -m 0755 "$BUILDER_DIR/baselines" "$BUILDER_DIR/work"
setfacl -R -m "u:$BUILD_USER:rwX" -d -m "u:$BUILD_USER:rwX" "$BUILDER_DIR/jobs"
setfacl -R -m "u:$SERVICE_USER:rwX" -d -m "u:$SERVICE_USER:rwX" "$BUILDER_DIR/jobs"

step "Installing the service"
sed -e "s|@BUILD_USER@|$BUILD_USER|g" -e "s|@PORTAL_DIR@|$PORTAL_DIR|g" \
  "$PORTAL_DIR/deploy/systemd/gns3-lab-portal-builder.service" > /etc/systemd/system/gns3-lab-portal-builder.service
chmod 644 /etc/systemd/system/gns3-lab-portal-builder.service
systemctl daemon-reload
systemctl enable gns3-lab-portal-builder.service
systemctl restart gns3-lab-portal-builder.service

cat <<DONE

APPLIANCE BUILDER IS SET UP.
  1. Put your stopped, application-free Ubuntu baseline disk (.qcow2) in $BUILDER_DIR/baselines/
     (a clean cloud-init-enabled Ubuntu server image; the portal lists what it finds there).
  2. Open Administration > Appliance builder, choose the baseline and an application, and start a build.
  The builder writes finished disks into GNS3's QEMU image folder (GNS3_IMAGES_DIR) as user $BUILD_USER.
  Check it with: systemctl status gns3-lab-portal-builder
DONE
