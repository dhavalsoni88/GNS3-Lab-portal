#!/usr/bin/env bash
# One-time setup that puts the GNS3 Lab Portal behind HTTPS on this server.
#
#   sudo bash scripts/setup-https.sh --fqdn lab.example.com [--san "lab,192.168.10.10"]
#
# It creates a first self-signed certificate (so Nginx can start), installs an Nginx site that serves
# HTTPS and redirects HTTP to it, and installs a small root helper plus a systemd path unit that reloads
# Nginx whenever the portal installs a new certificate. After this, certificates are created, requested
# (CSR), uploaded and replaced from Administration > HTTPS in the portal: no more root access is needed.
#
# An Nginx site that already forwards to the portal (127.0.0.1:8088) would clash with the new one. The
# script lists it and stops unless you add --replace-existing, which moves it to a backup folder.
# Safe to re-run: it keeps the certificate that is installed.
set -euo pipefail

PORTAL_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
SERVICE_USER=www-data
FQDN=""
SANS=""
REPLACE=0
STAMP=$(date +%Y%m%d-%H%M%S)
usage() { echo "Usage: sudo bash $0 --fqdn lab.example.com [--san name,ip,...] [--service-user www-data] [--replace-existing]" >&2; exit 2; }
fail() { printf '\nSETUP STOPPED: %s\n' "$*" >&2; exit 1; }
step() { printf '\n==> %s\n' "$*"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --fqdn) FQDN="${2:-}"; shift 2 ;;
    --san) SANS="${2:-}"; shift 2 ;;
    --service-user) SERVICE_USER="${2:-}"; shift 2 ;;
    --replace-existing) REPLACE=1; shift ;;
    *) usage ;;
  esac
done
[[ $EUID -eq 0 ]] || fail "run as root: sudo bash $0 ..."
[[ $FQDN =~ ^[A-Za-z0-9]([A-Za-z0-9.-]{0,61}[A-Za-z0-9])?$ ]] || usage
[[ -z $SANS || $SANS =~ ^[A-Za-z0-9.:,*-]+$ ]] || fail "--san may only contain names, IP addresses, commas, dots, colons, hyphens and *."
command -v nginx >/dev/null || fail "Nginx is not installed (sudo apt install nginx)."
command -v openssl >/dev/null || fail "openssl is not installed."
id "$SERVICE_USER" >/dev/null 2>&1 || fail "user $SERVICE_USER does not exist (use --service-user)."

ENV_FILE="$PORTAL_DIR/.env"
[[ -f $ENV_FILE ]] || fail "$ENV_FILE not found."
env_get() {  # last KEY=value in .env, without surrounding quotes
  local line
  line=$(grep -E "^[[:space:]]*$1=" "$ENV_FILE" | tail -n 1 || true)
  line=${line#*=}; line=${line%\"}; line=${line#\"}; line=${line%\'}; line=${line#\'}
  printf '%s' "$line"
}
env_set() {
  sed -i "/^[[:space:]]*$1=/d" "$ENV_FILE"
  printf '%s=%s\n' "$1" "$2" >> "$ENV_FILE"
}
TLS_DIR=$(env_get PORTAL_TLS_DIR)
TLS_DIR=${TLS_DIR:-$PORTAL_DIR/data/tls}
CONF=/etc/nginx/conf.d/gns3-lab-portal.conf
BACKUP_DIR=/etc/nginx/gns3-lab-portal-backup-$STAMP

step "Looking for an Nginx site that already forwards to the portal"
mapfile -t existing < <(grep -rlE 'proxy_pass[[:space:]]+https?://(127\.0\.0\.1|localhost):8088' /etc/nginx 2>/dev/null | grep -v "^$CONF\$" || true)
while IFS= read -r link; do   # sites-enabled links to those files must move too, or they would dangle
  target=$(readlink -f "$link")
  for file in "${existing[@]}"; do [[ $target == "$(readlink -f "$file")" ]] && existing+=("$link"); done
done < <(find /etc/nginx -type l 2>/dev/null)
if [[ ${#existing[@]} -gt 0 && $REPLACE -ne 1 ]]; then
  printf '  %s\n' "${existing[@]}"
  fail "these Nginx files already serve the portal and would clash. Check them, then run again with --replace-existing to move them to a backup folder."
fi

moved=()
restore() {
  printf '\nRestoring the previous Nginx files.\n' >&2
  if [[ -f $CONF.before ]]; then mv -f "$CONF.before" "$CONF"; else rm -f "$CONF"; fi
  for entry in "${moved[@]}"; do
    original=${entry%%|*}; saved=${entry#*|}
    if [[ -L $saved ]]; then cp -a "$saved" "$original"; else mv -f "$saved" "$original"; fi
  done
}
for file in "${existing[@]}"; do
  mkdir -p "$BACKUP_DIR"
  saved="$BACKUP_DIR/$(echo "$file" | tr / _)"
  echo "  moving $file to $saved"
  if [[ -L $file ]]; then
    cp -a "$file" "$saved"; rm -f "$file"        # a link in sites-enabled: the real file stays in sites-available
  else
    mv "$file" "$saved"
  fi
  moved+=("$file|$saved")
done

step "Preparing the certificate folder $TLS_DIR"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0750 "$TLS_DIR" "$TLS_DIR/active"
if [[ ! -s $TLS_DIR/active/fullchain.pem || ! -s $TLS_DIR/active/privkey.pem ]]; then
  san="DNS:$FQDN"
  for item in ${SANS//,/ }; do
    if [[ $item =~ ^[0-9.]+$ || $item == *:* ]]; then san+=",IP:$item"; else san+=",DNS:$item"; fi
  done
  echo "  creating a first self-signed certificate for $san (replace it from the portal's HTTPS page)"
  openssl req -x509 -newkey rsa:2048 -nodes -days 365 -subj "/CN=$FQDN" -addext "subjectAltName=$san" \
    -keyout "$TLS_DIR/active/privkey.pem" -out "$TLS_DIR/active/fullchain.pem" 2>/dev/null
  chown "$SERVICE_USER:$SERVICE_USER" "$TLS_DIR/active/privkey.pem" "$TLS_DIR/active/fullchain.pem"
  chmod 600 "$TLS_DIR/active/privkey.pem"; chmod 644 "$TLS_DIR/active/fullchain.pem"
else
  echo "  keeping the certificate that is already installed"
fi

step "Installing the Nginx site"
[[ -f $CONF ]] && cp -a "$CONF" "$CONF.before"
sed -e "s|@FQDN@|$FQDN|g" -e "s|@TLS_DIR@|$TLS_DIR|g" "$PORTAL_DIR/deploy/nginx/gns3-lab-portal-https.conf" > "$CONF"
[[ -e /proc/net/if_inet6 ]] && sed -i 's/^\([[:space:]]*\)#ipv6# /\1/' "$CONF"
if ! nginx -t; then
  restore
  fail "Nginx did not accept the new configuration (output above). Nothing was changed."
fi
rm -f "$CONF.before"

step "Installing the reload helper and its systemd units"
install -d -m 0755 /usr/local/libexec
install -m 0755 -o root -g root "$PORTAL_DIR/scripts/apply-tls.sh" /usr/local/libexec/gns3-lab-portal-apply-tls
for unit in gns3-lab-portal-tls.path gns3-lab-portal-tls.service; do
  sed "s|@TLS_DIR@|$TLS_DIR|g" "$PORTAL_DIR/deploy/systemd/$unit" > "/etc/systemd/system/$unit"
  chmod 644 "/etc/systemd/system/$unit"
done
: > "$TLS_DIR/reload-request"
touch "$TLS_DIR/nginx-managed"
chown "$SERVICE_USER:$SERVICE_USER" "$TLS_DIR/reload-request" "$TLS_DIR/nginx-managed"
systemctl daemon-reload
systemctl enable --now gns3-lab-portal-tls.path

step "Pointing the portal at its HTTPS address"
cp -a "$ENV_FILE" "$ENV_FILE.backup-$STAMP"
chmod 600 "$ENV_FILE.backup-$STAMP"
env_set PORTAL_PUBLIC_URL "https://$FQDN"
sed -i '/^[[:space:]]*PORTAL_COOKIE_SECURE=/d' "$ENV_FILE"   # HTTPS-only cookies are the default once PORTAL_PUBLIC_URL is https://

step "Starting Nginx and restarting the portal"
if systemctl is-active --quiet nginx; then systemctl reload nginx; else systemctl enable --now nginx; fi
systemctl restart gns3-lab-portal 2>/dev/null || echo "  (the portal service was not restarted; restart it so it reads .env)"

cat <<EOF

HTTPS IS SET UP.
  Open https://$FQDN/ (a self-signed certificate shows a browser warning until you replace or trust it).
  Replace the certificate in the portal: Administration > HTTPS. Choose a signing request (CSR) for a
  certificate authority, upload a certificate and key, or make another self-signed one.
  Port 443 must be reachable (for example: sudo ufw allow 443/tcp). Port 3080 (GNS3) stays closed to users.
  Previous .env saved as $ENV_FILE.backup-$STAMP.
EOF
[[ ${#moved[@]} -eq 0 ]] || echo "  Replaced Nginx files are in $BACKUP_DIR."
