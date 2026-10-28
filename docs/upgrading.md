# Upgrading

## Routine update

Back up first, and never replace `.env`: it holds your passwords, paths and
settings.

```bash
sudo systemctl stop gns3-lab-portal
sudo cp -a /opt/gns3-lab-portal/.env /opt/gns3-lab-portal/.env.backup
sudo cp -a /opt/gns3-lab-portal/data /opt/gns3-lab-portal/data.backup-$(date +%Y%m%d)

sudo git -C /opt/gns3-lab-portal pull --ff-only
sudo /opt/gns3-lab-portal/.venv/bin/pip install -r /opt/gns3-lab-portal/requirements.txt
sudo install -m 644 /opt/gns3-lab-portal/deploy/systemd/gns3-lab-portal.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl start gns3-lab-portal
```

On SQLite the schema upgrades itself at start-up. On MariaDB, apply
migrations and refresh grants before starting:

```bash
sudo bash /opt/gns3-lab-portal/scripts/setup-mariadb.sh
```

The script is safe to re-run. It keeps existing passwords.

Hard-refresh browsers after an update to load the new stylesheet.

## Upgrading from v8.x to v9.0

Do this on a test system first. Most of it is automatic:

- **Roles:** Administrators become Platform Admins and users become Lab
  Editors.
- **Passwords:** upgraded to Argon2id at each user's next sign-in.
- **Sign-in:** all users sign in again, because sessions do not survive a
  restart.
- **Interface:** the new layout replaces the old dashboard. All existing
  data, labs, backups, templates and shares carry over.

Before starting v9.0:

1. Install the new requirements, including `pkg-config libxmlsec1-dev` if
   `pip` cannot find an `xmlsec` wheel.
2. Make sure `PORTAL_SECRET_KEY` is a long random value. It now also
   encrypts stored directory passwords.
3. Check `GNS3_URL` is set in `.env`. Its built-in default changed to
   `http://127.0.0.1:3080`.
4. The service file moved to `deploy/systemd/gns3-lab-portal.service` and
   the snapshot script to `scripts/migrate-snapshots-to-hdd-store.sh`.

Behaviour changes to tell users about:

- Restoring or deleting backups, and deleting a lab, now need the lab's
  owner or a Lab Manager. Users a lab is shared with can no longer do these.
- Lab creation moved to **New lab**, templates to **Templates**, the audit
  trail to **Audit log** and the server monitor to **Server status**.

Optional: move to MariaDB ([database.md](database.md)) and connect Active
Directory or SSO ([authentication.md](authentication.md)).

## Earlier versions

Version-specific steps for v8.x releases are listed under **Upgrade notes**
in the [changelog](../CHANGELOG.md).
