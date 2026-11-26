# Upgrading

## The one-step upgrade

On every existing server, still on SQLite or already on MariaDB, run:

```bash
sudo git -C /opt/gns3-lab-portal pull --ff-only
sudo bash /opt/gns3-lab-portal/scripts/upgrade.sh
```

[`scripts/upgrade.sh`](../scripts/upgrade.sh) works out which kind of server
it is on and then:

1. backs up `.env` (and the installed service file) with a timestamp;
2. replaces a placeholder or short `PORTAL_SECRET_KEY` with a random one;
3. installs the Python requirements and prepares MariaDB with
   `setup-mariadb.sh` (installs it if needed, applies migrations, refreshes
   grants);
4. stops the portal;
5. **SQLite servers only:** copies every row to MariaDB and verifies it, then
   switches `.env` to MariaDB. If the copy does not verify, it stops with
   `.env` unchanged;
6. installs the new service file and starts the portal, then checks that it
   answers.

It is safe to re-run. Once `.env` says MariaDB, it never copies from SQLite
again, so rows deleted in MariaDB cannot come back. It prints each step, and
on failure says what to do and how to go back.

Before you run it on a SQLite server, read [What keeps the move safe](#what-keeps-the-move-safe)
and, if you can, rehearse on a copy. If it replaced your secret key and you
use Active Directory, re-enter the service-account password under
**Administration › Identity**. The sections below describe the same steps
by hand, for a remote MariaDB or a customised install.

## Routine update (by hand)

Back up first, and never replace `.env`: it holds your passwords, paths and
settings.

```bash
sudo systemctl stop gns3-lab-portal
sudo cp -a /opt/gns3-lab-portal/.env /opt/gns3-lab-portal/.env.backup
sudo mariadb-dump --single-transaction --routines --triggers gns3_lab_portal | gzip > /root/gns3_lab_portal-$(date +%Y%m%d).sql.gz

sudo git -C /opt/gns3-lab-portal pull --ff-only
sudo /opt/gns3-lab-portal/.venv/bin/pip install -r /opt/gns3-lab-portal/requirements.txt
sudo bash /opt/gns3-lab-portal/scripts/setup-mariadb.sh   # applies new migrations, refreshes grants
sudo install -m 644 /opt/gns3-lab-portal/deploy/systemd/gns3-lab-portal.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl start gns3-lab-portal
```

`setup-mariadb.sh` is safe to re-run and keeps existing passwords. The
portal refuses to start while a migration is pending, so a skipped step
shows up at once in `journalctl -u gns3-lab-portal`.

Hard-refresh browsers after an update to load the new stylesheet.

## Upgrading from v8.x to v9.0

v9.0 **runs only on MariaDB**. Every v8.x install uses SQLite, so the move
below is part of this upgrade. Do it on a test system first.

Most of the rest is automatic:

- **Roles:** Administrators become Platform Admins and users become Lab
  Editors.
- **Passwords:** upgraded to Argon2id at each user's next sign-in.
- **Sign-in:** all users sign in again, because sessions are not copied.
- **Interface:** the new layout replaces the old dashboard. All existing
  data, labs, backups, templates and shares carry over.

Check these settings before you start, because v9.0 refuses to start
without them:

1. `PORTAL_SECRET_KEY` is a random value of 32 or more characters, not the
   placeholder from `.env.example`. **Keep the value you already have** if it
   is strong. Changing it signs everyone out and makes stored directory
   passwords unreadable.
2. `PORTAL_AUTH_ENABLED` now defaults to `true`. Leave it on.
3. `GNS3_URL` is set in `.env`. Its built-in default changed to
   `http://127.0.0.1:3080`.
4. The service file moved to `deploy/systemd/gns3-lab-portal.service` and
   the snapshot script to `scripts/migrate-snapshots-to-hdd-store.sh`.
   Install the new service file: it keeps one worker and keeps upload
   buffers off the root disk.

Install the new requirements, including `pkg-config libxmlsec1-dev` if `pip`
cannot find an `xmlsec` wheel.

Behaviour changes to tell users about:

- Restoring or deleting backups, and deleting a lab, now need the lab's
  owner or a Lab Manager. Users a lab is shared with can no longer do these.
- Lab creation moved to **New lab**, templates to **Templates**, the audit
  trail to **Audit log** and the server monitor to **Server status**.
- After five failed sign-ins, a name is locked for 15 minutes from that
  address.

## Moving from SQLite to MariaDB

This applies to every v8.x install, and to v9 preview installs that ran on
SQLite. `scripts/upgrade.sh` does all of this in one go; the steps below are
the manual equivalent and explain what it does.

### What keeps the move safe

| Risk | Safeguard |
|---|---|
| The old data is damaged | The script opens `audit.db` **read-only** and first writes a dated backup copy next to it. |
| Rows are silently lost | After copying, the script looks up every SQLite row in MariaDB by its key and fails if one is missing. |
| A failed or interrupted run | Re-run it. It copies only rows that are not there yet (`INSERT IGNORE`) and never overwrites rows already in MariaDB. |
| The portal starts on an empty MariaDB and hides every owner and share | v9.0 refuses to start while MariaDB has no users and `audit.db` still exists. It also refuses `PORTAL_DB_BACKEND=sqlite`. |
| Unclear who changed what | MariaDB's change history records every copied row with the actor `sqlite-migration`. |

What is copied: users, roles, identity providers and group mappings, lab
owners, lab and backup shares, retained backups, templates and their access
lists, and the whole audit log. Sessions are not copied, so everyone signs
in again. Lab files, snapshots, retained-backup and template archives stay
where they are on disk; only their records move.

**Downtime:** the portal is stopped from step 3 to step 5. The script checks
every row it copies one by one, so the time grows with the audit log; the
rehearsal in step 1 tells you how long yours takes. GNS3 and running labs are
not affected.

### 1. Rehearse on a copy (recommended)

Copy `/opt/gns3-lab-portal/data/audit.db` to a test server with MariaDB,
then run the script there with `--dry-run` and again without it. It lists
every table with its row count. Keep that output to compare with production.

### 2. Prepare MariaDB, with the portal still running

Install the new code and its requirements, then prepare MariaDB. Nothing
here touches the SQLite database. If the portal restarts by accident, v9.0
stops with an error instead of starting empty. (`pip` may update libraries
that the running portal loads on demand, such as the SAML library. If you
use SSO, you can run this step after stopping the portal in step 3.)

```bash
sudo cp -a /opt/gns3-lab-portal/.env /opt/gns3-lab-portal/.env.backup
sudo git -C /opt/gns3-lab-portal pull --ff-only
sudo /opt/gns3-lab-portal/.venv/bin/pip install -r /opt/gns3-lab-portal/requirements.txt
sudo bash /opt/gns3-lab-portal/scripts/setup-mariadb.sh
```

Keep the `PORTAL_DB_*` lines the script prints.

### 3. Stop the portal and check what will be copied

```bash
sudo systemctl stop gns3-lab-portal
sudo env PORTAL_DB_HOST=localhost \
  PORTAL_DB_MIGRATE_USER=gns3_portal_migrate \
  PORTAL_DB_MIGRATE_PASSWORD_FILE=/etc/gns3-lab-portal/db-migrate-password \
  /opt/gns3-lab-portal/.venv/bin/python /opt/gns3-lab-portal/scripts/migrate-sqlite-to-mariadb.py \
  --sqlite /opt/gns3-lab-portal/data/audit.db --dry-run
```

### 4. Copy and verify

Run the same command without `--dry-run`. It must end with
`Migration verified.` and every table must show `OK`. If it shows
`MISSING` or stops with an error, fix the cause (usually a permission or a
full disk) and run it again. Re-running is safe.

### 5. Switch `.env` and start

In `/opt/gns3-lab-portal/.env`, replace the old database lines
(`PORTAL_DB_BACKEND=sqlite`, `AUDIT_DB=…`) with the lines `setup-mariadb.sh`
printed:

```env
PORTAL_DB_BACKEND=mariadb
PORTAL_DB_HOST=localhost
PORTAL_DB_NAME=gns3_lab_portal
PORTAL_DB_USER=gns3_portal_app
PORTAL_DB_PASSWORD_FILE=/etc/gns3-lab-portal/db-app-password
```

Install the new service file, then start:

```bash
sudo install -m 644 /opt/gns3-lab-portal/deploy/systemd/gns3-lab-portal.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl start gns3-lab-portal
sudo journalctl -u gns3-lab-portal -n 30 --no-pager
```

### 6. Check

- Sign in as a local Platform Admin. **Users** lists every account with its
  roles.
- **Labs** shows the right owner on each lab, and a user you share with
  still sees the shared lab.
- **Audit log** shows entries from before the move.
- **Administration › Identity** lists your directory and SSO providers. Run
  **Test** on the directory: this confirms the stored bind password still
  decrypts with your `PORTAL_SECRET_KEY`.

Leave `audit.db` and its `.pre-mariadb-*` copy in place as a read-only
archive. The portal never writes to them again.

### Going back

**Before users have made changes in MariaDB**, you can return to the old
version:

```bash
sudo systemctl stop gns3-lab-portal
sudo cp -a /opt/gns3-lab-portal/.env.backup /opt/gns3-lab-portal/.env
sudo git -C /opt/gns3-lab-portal checkout <previous-version-commit>
sudo /opt/gns3-lab-portal/.venv/bin/pip install -r /opt/gns3-lab-portal/requirements.txt
sudo systemctl start gns3-lab-portal
```

The script never changes `audit.db`, so the old version finds everything as
it was. Anything done after the switch (new users, shares, audit events)
exists only in MariaDB. Once users have worked on v9.0, fix the problem and
stay on MariaDB instead of going back.

## Earlier versions

Version-specific steps for v8.x releases are listed under **Upgrade notes**
in the [changelog](../CHANGELOG.md).
