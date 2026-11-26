# Installation

This guide installs the portal on Ubuntu, on the **same server as the GNS3
controller**. That is the simplest layout and the one the examples use. The
portal can run on a separate server too, but showing backup sizes,
downloading backups and creating labs from them then need read access to the
GNS3 project and snapshot folders over a network mount.

Examples use `192.0.2.10` for the server and `/HDD_STORE` for a second disk.
Replace them with your own values.

## Before you begin

- GNS3 server is installed and running, and its API answers on port 3080.
- Ubuntu 22.04 or 24.04 with Python 3.10 or newer, systemd, and a user that
  can run `sudo`.
- An HTTPS reverse proxy (Nginx, Caddy or similar) to publish the portal.
  The service itself listens only on `127.0.0.1:8088`.
- Firewall port 3080 so ordinary users cannot reach the GNS3 API directly.

The portal service runs as `www-data`. It needs write access to its own data
and log directories, and read access to GNS3's projects and snapshots.

## 1. Download the portal

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip acl pkg-config libxmlsec1-dev
sudo git clone https://github.com/dhavalsoni88/GNS3-Lab-portal.git /opt/gns3-lab-portal
cd /opt/gns3-lab-portal
```

`pkg-config` and `libxmlsec1-dev` are needed only if no prebuilt `xmlsec`
wheel exists for your Python version; installing them is harmless otherwise.

## 2. Create the Python environment

```bash
sudo python3 -m venv /opt/gns3-lab-portal/.venv
sudo /opt/gns3-lab-portal/.venv/bin/pip install -r /opt/gns3-lab-portal/requirements.txt
sudo cp /opt/gns3-lab-portal/.env.example /opt/gns3-lab-portal/.env
sudo chmod 640 /opt/gns3-lab-portal/.env
sudo chgrp www-data /opt/gns3-lab-portal/.env
```

## 2a. Prepare MariaDB

The portal stores everything in MariaDB 10.6 or newer. This script installs
`mariadb-server` if needed, creates the database and three least-privilege
accounts, turns on the server audit log and applies the schema. It prints the
`PORTAL_DB_*` lines for `.env`, and is safe to re-run.

```bash
sudo bash /opt/gns3-lab-portal/scripts/setup-mariadb.sh
```

Upgrading a server that already runs the portal on SQLite? Stop here and
follow [Moving from SQLite to MariaDB](upgrading.md#moving-from-sqlite-to-mariadb) instead.

## 3. Configure `.env`

Edit `/opt/gns3-lab-portal/.env`. A typical same-host configuration:

```env
GNS3_URL=http://127.0.0.1:3080
GNS3_WEBUI_URL=http://192.0.2.10:3080
GNS3_USERNAME=admin
GNS3_PASSWORD=your-gns3-password

PORTAL_AUTH_ENABLED=true
PORTAL_USERNAME=portaladmin
PORTAL_PASSWORD=a-strong-initial-admin-password
PORTAL_SECRET_KEY=paste-the-output-of-openssl-rand-hex-32

PORTAL_DB_BACKEND=mariadb
PORTAL_DB_HOST=localhost
PORTAL_DB_NAME=gns3_lab_portal
PORTAL_DB_USER=gns3_portal_app
PORTAL_DB_PASSWORD_FILE=/etc/gns3-lab-portal/db-app-password

GNS3_PROJECTS_DIR=/home/gns3/GNS3/projects
GNS3_SNAPSHOTS_DIR=/HDD_STORE/gns3-snapshots
RETAINED_BACKUP_DIR=/HDD_STORE/my-lab-retained-backups
LAB_TEMPLATE_DIR=/HDD_STORE/my-lab-templates
PORTAL_LOG_DIR=/var/log/gns3-lab-portal
```

Generate the secret key with `openssl rand -hex 32`. It signs sessions and
encrypts stored directory passwords, so keep it stable. The portal will not
start with a placeholder or a key shorter than 32 characters.

`PORTAL_USERNAME` and `PORTAL_PASSWORD` create the first Platform Admin only
when the portal database is first created. Change that password in the
portal afterwards; editing `.env` later does not change an existing account.

Every setting is described in [configuration.md](configuration.md).

## 4. Create data and log directories

```bash
sudo install -d -o www-data -g www-data -m 0750 \
  /opt/gns3-lab-portal/data \
  /opt/gns3-lab-portal/data/uploads \
  /opt/gns3-lab-portal/data/avatars \
  /var/log/gns3-lab-portal \
  /HDD_STORE/my-lab-retained-backups \
  /HDD_STORE/my-lab-templates
```

Check the second disk is really mounted (`findmnt /HDD_STORE`) before using it
for backups.

## 5. Give the portal read access to GNS3 backups

The portal reads native snapshots to show their sizes, download them and
create labs from them. Files stay owned by `gns3`; `www-data` gets read
access through ACLs:

```bash
sudo setfacl -m u:www-data:--x /home/gns3
sudo setfacl -m u:www-data:rx /home/gns3/GNS3
sudo setfacl -R -m u:www-data:rX /home/gns3/GNS3/projects
```

If snapshots live on a separate disk, also follow [storage.md](storage.md).

## 6. Install and start the service

```bash
sudo install -m 644 /opt/gns3-lab-portal/deploy/systemd/gns3-lab-portal.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now gns3-lab-portal
sudo systemctl status gns3-lab-portal --no-pager
```

## 7. Publish it over HTTPS

Copy [`deploy/nginx/gns3-lab-portal.conf`](../deploy/nginx/gns3-lab-portal.conf)
to `/etc/nginx/sites-available/`, set your host name and certificate paths,
enable it, and reload Nginx. Then add to `.env` and restart the portal:

```env
PORTAL_COOKIE_SECURE=true
PORTAL_PUBLIC_URL=https://lab-portal.example.com
```

`PORTAL_COOKIE_SECURE` keeps the session cookie off plain HTTP.
`PORTAL_PUBLIC_URL` is required for single sign-on.

## 8. Verify

```bash
curl -I http://127.0.0.1:8088/login
sudo journalctl -u gns3-lab-portal -n 50 --no-pager
sudo tail -n 50 /var/log/gns3-lab-portal/portal.log
```

Sign in with the initial administrator, change its password under
**My profile**, then create users or connect a directory under
**Administration**.

## Next steps

- Read the [administrator guide](admin-guide.md) for day-to-day tasks.
- Connect Active Directory or SSO: [authentication.md](authentication.md).
