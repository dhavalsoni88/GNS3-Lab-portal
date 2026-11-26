#!/usr/bin/env bash
# Prepare a local MariaDB server for the GNS3 Lab Portal.
#
# Creates the database and three least-privilege accounts, enables the MariaDB
# server_audit plugin, stores generated passwords in root-owned secret files,
# and applies the portal schema migrations.
#
#   gns3_portal_app      runtime: read/write lab tables; audit tables are
#                        INSERT/SELECT only (append-only, enforced by triggers)
#   gns3_portal_migrate  schema migrations only; used by the installer
#   gns3_portal_auditor  read-only access to audit tables for reviewers/SIEM
#
# Usage: sudo bash scripts/setup-mariadb.sh [--portal-dir /opt/gns3-lab-portal]
# Re-running is safe: existing passwords are kept and grants are re-applied.
set -euo pipefail

PORTAL_DIR=/opt/gns3-lab-portal
SERVICE_USER=www-data
DB_NAME=gns3_lab_portal
SECRET_DIR=/etc/gns3-lab-portal
while [[ $# -gt 0 ]]; do
  case "$1" in
    --portal-dir) PORTAL_DIR="$2"; shift 2 ;;
    --service-user) SERVICE_USER="$2"; shift 2 ;;
    --db-name) DB_NAME="$2"; shift 2 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done
[[ $EUID -eq 0 ]] || { echo "Run as root (sudo)." >&2; exit 1; }
[[ "$DB_NAME" =~ ^[A-Za-z0-9_]+$ ]] || { echo "Invalid database name." >&2; exit 2; }

if ! command -v mariadb >/dev/null; then
  apt-get update -q
  DEBIAN_FRONTEND=noninteractive apt-get install -y -q mariadb-server
fi
systemctl enable --now mariadb

install -d -m 0750 -o root -g "$SERVICE_USER" "$SECRET_DIR"
secret() {  # secret <name> <group>: create once, then reuse.
  local file="$SECRET_DIR/$1"
  if [[ ! -s "$file" ]]; then
    (umask 077; head -c 32 /dev/urandom | base64 | tr -d '/+=' | head -c 40 > "$file")
  fi
  chown root:"$2" "$file"; chmod 0640 "$file"
  cat "$file"
}
APP_PW=$(secret db-app-password "$SERVICE_USER")
MIGRATE_PW=$(secret db-migrate-password root)
AUDITOR_PW=$(secret db-auditor-password root)

install -d -o mysql -g adm -m 2750 /var/log/mysql
# Server-level audit log: connections, failed logins, schema (DDL) and
# privilege (DCL) changes by every account, written to a rotated file.
cat > /etc/mysql/mariadb.conf.d/60-gns3-portal-audit.cnf <<'CNF'
[mariadb]
plugin_load_add = server_audit
server_audit = FORCE_PLUS_PERMANENT
server_audit_logging = ON
server_audit_events = CONNECT,QUERY_DDL,QUERY_DCL
server_audit_output_type = file
server_audit_file_path = /var/log/mysql/server_audit.log
server_audit_file_rotate_size = 104857600
server_audit_file_rotations = 30
CNF
systemctl restart mariadb

mariadb --protocol=socket <<SQL
CREATE DATABASE IF NOT EXISTS \`$DB_NAME\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS 'gns3_portal_app'@'localhost' IDENTIFIED BY '$APP_PW';
CREATE USER IF NOT EXISTS 'gns3_portal_migrate'@'localhost' IDENTIFIED BY '$MIGRATE_PW';
CREATE USER IF NOT EXISTS 'gns3_portal_auditor'@'localhost' IDENTIFIED BY '$AUDITOR_PW';
ALTER USER 'gns3_portal_app'@'localhost' IDENTIFIED BY '$APP_PW';
ALTER USER 'gns3_portal_migrate'@'localhost' IDENTIFIED BY '$MIGRATE_PW';
ALTER USER 'gns3_portal_auditor'@'localhost' IDENTIFIED BY '$AUDITOR_PW';
GRANT ALL PRIVILEGES ON \`$DB_NAME\`.* TO 'gns3_portal_migrate'@'localhost';
FLUSH PRIVILEGES;
SQL

cd "$PORTAL_DIR"
PY="$PORTAL_DIR/.venv/bin/python"; [[ -x "$PY" ]] || PY=python3
PORTAL_DB_BACKEND=mariadb PORTAL_DB_HOST=localhost PORTAL_DB_NAME="$DB_NAME" \
PORTAL_DB_MIGRATE_USER=gns3_portal_migrate PORTAL_DB_MIGRATE_PASSWORD_FILE="$SECRET_DIR/db-migrate-password" \
  "$PY" portal_db.py migrate

# Table-level grants must follow the migrations that create the tables.
AUDIT_TABLES="audit_log db_change_log"
{
  echo "REVOKE ALL PRIVILEGES, GRANT OPTION FROM 'gns3_portal_app'@'localhost';"
  echo "REVOKE ALL PRIVILEGES, GRANT OPTION FROM 'gns3_portal_auditor'@'localhost';"
  mariadb --protocol=socket -N -e "SELECT table_name FROM information_schema.tables WHERE table_schema='$DB_NAME'" | while read -r table; do
    if [[ " $AUDIT_TABLES " == *" $table "* ]]; then
      echo "GRANT SELECT, INSERT ON \`$DB_NAME\`.\`$table\` TO 'gns3_portal_app'@'localhost';"
      echo "GRANT SELECT ON \`$DB_NAME\`.\`$table\` TO 'gns3_portal_auditor'@'localhost';"
    elif [[ "$table" == schema_migrations ]]; then
      echo "GRANT SELECT ON \`$DB_NAME\`.\`$table\` TO 'gns3_portal_app'@'localhost';"
    else
      echo "GRANT SELECT, INSERT, UPDATE, DELETE ON \`$DB_NAME\`.\`$table\` TO 'gns3_portal_app'@'localhost';"
    fi
  done
  echo "FLUSH PRIVILEGES;"
} | mariadb --protocol=socket

cat <<EOF
MariaDB is ready for the portal. Add these lines to $PORTAL_DIR/.env:

PORTAL_DB_BACKEND=mariadb
PORTAL_DB_HOST=localhost
PORTAL_DB_NAME=$DB_NAME
PORTAL_DB_USER=gns3_portal_app
PORTAL_DB_PASSWORD_FILE=$SECRET_DIR/db-app-password

To move an existing SQLite database (v8.x, or v9 previews), follow
docs/upgrading.md. In short: stop the portal, then run:

  sudo systemctl stop gns3-lab-portal
  sudo env PORTAL_DB_HOST=localhost PORTAL_DB_NAME=$DB_NAME \\
    PORTAL_DB_MIGRATE_USER=gns3_portal_migrate \\
    PORTAL_DB_MIGRATE_PASSWORD_FILE=$SECRET_DIR/db-migrate-password \\
    $PY $PORTAL_DIR/scripts/migrate-sqlite-to-mariadb.py --sqlite $PORTAL_DIR/data/audit.db
EOF
