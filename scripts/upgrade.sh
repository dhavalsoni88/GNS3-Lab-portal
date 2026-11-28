#!/usr/bin/env bash
# Upgrade a GNS3 Lab Portal server to the code you just pulled.
#
#   sudo git -C /opt/gns3-lab-portal pull --ff-only
#   sudo bash /opt/gns3-lab-portal/scripts/upgrade.sh
#
# Works for both kinds of server:
#   - still on SQLite (v8.x or a v9 preview): installs MariaDB, copies and
#     verifies every row, switches .env to MariaDB. audit.db is never changed.
#   - already on MariaDB: applies new migrations and refreshes grants.
# Safe to re-run. Every step that changes something says so first.
set -euo pipefail

PORTAL_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
SERVICE=gns3-lab-portal
SERVICE_USER=www-data
while [[ $# -gt 0 ]]; do
  case "$1" in
    --service-user) SERVICE_USER="$2"; shift 2 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

ENV_FILE="$PORTAL_DIR/.env"
PY="$PORTAL_DIR/.venv/bin/python"
STAMP=$(date +%Y%m%d-%H%M%S)
step() { printf '\n==> %s\n' "$*"; }
fail() { printf '\nUPGRADE STOPPED: %s\n' "$*" >&2; exit 1; }
env_get() {  # last KEY=value in .env, without surrounding quotes
  local line
  line=$(grep -E "^[[:space:]]*$1=" "$ENV_FILE" | tail -n 1 || true)
  line=${line#*=}; line=${line%\"}; line=${line#\"}; line=${line%\'}; line=${line#\'}
  printf '%s' "$line"
}
env_set() {  # replace every KEY= line with one KEY=value at the end
  sed -i "/^[[:space:]]*$1=/d" "$ENV_FILE"
  printf '%s=%s\n' "$1" "$2" >> "$ENV_FILE"
}

[[ $EUID -eq 0 ]] || fail "run as root: sudo bash $0"
[[ -f "$ENV_FILE" ]] || fail "$ENV_FILE not found. For a new server follow docs/installation.md."

step "Checking the current setup"
# v8.x had no PORTAL_DB_BACKEND line: an .env without one is a SQLite install.
BACKEND=$(env_get PORTAL_DB_BACKEND); BACKEND=${BACKEND:-sqlite}
SQLITE=$(env_get AUDIT_DB); SQLITE=${SQLITE:-./data/audit.db}
[[ "$SQLITE" = /* ]] || SQLITE="$PORTAL_DIR/${SQLITE#./}"
if [[ "$BACKEND" == mariadb ]]; then
  MODE=update
  DB_HOST=$(env_get PORTAL_DB_HOST); DB_NAME=$(env_get PORTAL_DB_NAME)
  [[ "${DB_HOST:-127.0.0.1}" == localhost ]] || fail "PORTAL_DB_HOST is '${DB_HOST:-127.0.0.1}'. This script manages a local MariaDB only; for a remote one run 'python3 portal_db.py migrate' with the migration account (docs/upgrading.md)."
  DB_NAME=${DB_NAME:-gns3_lab_portal}
  echo "MariaDB install: routine update of database '$DB_NAME'."
elif [[ -f "$SQLITE" ]]; then
  MODE=migrate; DB_NAME=gns3_lab_portal
  echo "SQLite install: data in $SQLITE will be copied to MariaDB."
else
  MODE=switch; DB_NAME=gns3_lab_portal
  echo "No SQLite database found at $SQLITE: the portal will start on an empty MariaDB."
fi
[[ -n "$(env_get GNS3_URL)" ]] || echo "WARNING: GNS3_URL is not set in .env; the default is now http://127.0.0.1:3080."
[[ "$(env_get PORTAL_COOKIE_SECURE)" == false ]] && echo "WARNING: .env has PORTAL_COOKIE_SECURE=false, so session cookies can travel over plain HTTP. Remove that line if the portal is served over HTTPS."

step "Backing up .env to .env.backup-$STAMP"
cp -a "$ENV_FILE" "$ENV_FILE.backup-$STAMP"
chmod 600 "$ENV_FILE.backup-$STAMP"  # holds the GNS3 and admin passwords

KEY=$(env_get PORTAL_SECRET_KEY)
if [[ ${#KEY} -lt 32 || "$KEY" == change-this-before-production || "$KEY" == replace-with-a-long-random-value ]]; then
  step "PORTAL_SECRET_KEY is a placeholder or too short: generating a new one"
  # The new key cannot decrypt directory bind passwords saved with the old one.
  env_set PORTAL_SECRET_KEY "$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
  KEY_ROTATED=1
fi

step "Installing system packages and Python requirements"
export DEBIAN_FRONTEND=noninteractive
apt-get install -y -q pkg-config libxmlsec1-dev >/dev/null || { apt-get update -q && apt-get install -y -q pkg-config libxmlsec1-dev >/dev/null; }
# Rebuild the venv if its Python is gone (e.g. after an OS Python upgrade).
"$PY" -c 'import sys' 2>/dev/null || python3 -m venv --clear "$PORTAL_DIR/.venv"
# "python -m pip", not .venv/bin/pip: a venv copied from elsewhere has a pip that installs into the original.
"$PY" -m pip install -q -r "$PORTAL_DIR/requirements.txt"
"$PY" -c 'import pymysql, fastapi' || fail "the Python requirements did not install into $PORTAL_DIR/.venv."

step "Preparing MariaDB (installs it if needed, applies migrations, refreshes grants)"
bash "$PORTAL_DIR/scripts/setup-mariadb.sh" --portal-dir "$PORTAL_DIR" --service-user "$SERVICE_USER" --db-name "$DB_NAME" >/dev/null

step "Stopping the portal"
systemctl stop "$SERVICE" 2>/dev/null || true

if [[ "$MODE" == migrate ]]; then
  # Only while .env still says SQLite: the portal has never written to MariaDB,
  # so re-running cannot bring back rows deleted there later.
  step "Copying SQLite data to MariaDB and checking every row"
  PORTAL_DB_HOST=localhost PORTAL_DB_NAME="$DB_NAME" \
  PORTAL_DB_MIGRATE_USER=gns3_portal_migrate PORTAL_DB_MIGRATE_PASSWORD_FILE=/etc/gns3-lab-portal/db-migrate-password \
    "$PY" "$PORTAL_DIR/scripts/migrate-sqlite-to-mariadb.py" --sqlite "$SQLITE" \
    || fail "the data copy did not verify. The portal is stopped and .env is unchanged (still SQLite); fix the error above and run this script again."
fi

if [[ "$MODE" != update ]]; then
  step "Switching .env to MariaDB"
  env_set PORTAL_DB_BACKEND mariadb
  env_set PORTAL_DB_HOST localhost
  env_set PORTAL_DB_NAME "$DB_NAME"
  env_set PORTAL_DB_USER gns3_portal_app
  env_set PORTAL_DB_PASSWORD_FILE /etc/gns3-lab-portal/db-app-password
  sed -i '/^[[:space:]]*PORTAL_DB_PASSWORD=/d' "$ENV_FILE"
fi
chmod 640 "$ENV_FILE"; chgrp "$SERVICE_USER" "$ENV_FILE"

step "Installing the service file and data folders"
UNIT=/etc/systemd/system/$SERVICE.service
[[ -f "$UNIT" ]] && cp -a "$UNIT" "$UNIT.backup-$STAMP"
install -m 644 "$PORTAL_DIR/deploy/systemd/$SERVICE.service" "$UNIT"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0750 "$PORTAL_DIR/data" "$PORTAL_DIR/data/uploads" "$PORTAL_DIR/data/avatars"
systemctl daemon-reload
if [[ -e /usr/local/libexec/gns3-lab-portal-apply-tls ]]; then   # HTTPS was set up with setup-https.sh
  install -m 0755 -o root -g root "$PORTAL_DIR/scripts/apply-tls.sh" /usr/local/libexec/gns3-lab-portal-apply-tls
fi
if [[ -e /etc/systemd/system/gns3-lab-portal-builder.service ]]; then   # builder was set up with setup-builder.sh
  build_user=$(sed -n 's/^User=//p' /etc/systemd/system/gns3-lab-portal-builder.service)
  sed -e "s|@BUILD_USER@|$build_user|g" -e "s|@PORTAL_DIR@|$PORTAL_DIR|g" "$PORTAL_DIR/deploy/systemd/gns3-lab-portal-builder.service" > /etc/systemd/system/gns3-lab-portal-builder.service
  install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0755 /var/lib/gns3-lab-portal-builder/recipes   # added in v9.2
  systemctl daemon-reload
  systemctl restart gns3-lab-portal-builder.service
fi

step "Starting the portal"
systemctl start "$SERVICE"
for _ in $(seq 1 30); do
  if curl -fs -o /dev/null http://127.0.0.1:8088/login; then
    printf '\nUPGRADE COMPLETE (%s). The portal answers on 127.0.0.1:8088.\n' "$MODE"
    [[ "$MODE" == migrate ]] && echo "Everyone signs in again. $SQLITE is kept unchanged as an archive."
    [[ -n "${KEY_ROTATED:-}" ]] && echo "A new PORTAL_SECRET_KEY was written to .env. If you use Active Directory, re-enter its service-account password under Administration > Identity."
    echo "Previous .env saved as $ENV_FILE.backup-$STAMP."
    # Publishing appliances writes disks into GNS3's QEMU image folder; say so if the portal cannot.
    IMAGES_DIR=$(env_get GNS3_IMAGES_DIR)
    if [[ -z "$IMAGES_DIR" ]]; then
      PROJECTS_DIR=$(env_get GNS3_PROJECTS_DIR)
      IMAGES_DIR="$(dirname "${PROJECTS_DIR:-/home/gns3/GNS3/projects}")/images/QEMU"
    fi
    runuser -u "$SERVICE_USER" -- test -w "$IMAGES_DIR" 2>/dev/null \
      || echo "NOTE: publishing appliances needs write access to $IMAGES_DIR. To allow it: sudo setfacl -R -m u:$SERVICE_USER:rwX $IMAGES_DIR"
    exit 0
  fi
  sleep 1
done
journalctl -u "$SERVICE" -n 30 --no-pager 2>/dev/null || true
fail "the portal did not answer within 30 seconds (log above). To go back: restore .env.backup-$STAMP, git checkout the previous commit, and start the service. See docs/upgrading.md#going-back."
