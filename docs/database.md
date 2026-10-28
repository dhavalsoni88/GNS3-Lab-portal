# Database

The portal stores accounts, roles, sessions, lab ownership, sharing,
templates, identity providers and the audit log in a database:

- **SQLite** is the default. It needs no setup and suits a single server.
- **MariaDB 10.6 or newer** is required for multi-server clusters and adds
  tamper-resistant auditing.

Choose one with `PORTAL_DB_BACKEND`. The application code is the same for
both: [`portal_db.py`](../portal_db.py) translates its SQL for MariaDB.

## Schema and migrations

| Migration | Contents |
|---|---|
| [`001_core.sql`](../database/mariadb/001_core.sql) | Core tables, one-to-one with the SQLite schema. |
| [`002_audit.sql`](../database/mariadb/002_audit.sql) | Audit request context, append-only triggers and the change history. |
| [`003_identity.sql`](../database/mariadb/003_identity.sql) | Sign-in sources, roles, identity providers and group mappings. |

On SQLite, the schema is created and upgraded automatically at start-up. On
MariaDB, migrations are applied once each, in order, and recorded in
`schema_migrations`:

```bash
python3 portal_db.py status    # list pending migrations
python3 portal_db.py migrate   # apply them (uses the migration account)
```

The portal refuses to start while MariaDB migrations are pending.

## Audit on MariaDB

- **Portal events** (`audit_log`) record user, action, target, source IP,
  browser, request ID, result and the connected MariaDB account.
- **Change history** (`db_change_log`) records the old and new values of
  every insert, update and delete on users, roles, group mappings, identity
  providers, lab ownership, shares, retained backups and templates. Each
  entry names the portal user, source IP and request ID behind it.
  - Password hashes are never copied; only a *password changed* flag is.
  - Identity provider settings are recorded as a SHA-256 hash, because they
    hold encrypted secrets.
- **Append-only:** triggers reject any update or delete on both tables, for
  every account including `root`. Purging old records means a DBA must first
  drop a trigger, and that drop is itself logged by the server audit plugin.
- **Server audit:** MariaDB's `server_audit` plugin writes connections, failed
  logins, schema changes and grant changes to
  `/var/log/mysql/server_audit.log`, with passwords masked.

## Accounts

[`scripts/setup-mariadb.sh`](../scripts/setup-mariadb.sh) creates these
accounts and stores their generated passwords in `/etc/gns3-lab-portal/`
with mode 0640:

| Account | Rights |
|---|---|
| `gns3_portal_app` | Portal runtime. Read/write on lab tables; insert and select only on the audit tables. No schema changes. |
| `gns3_portal_migrate` | Schema migrations only. Owns the triggers. |
| `gns3_portal_auditor` | Read-only access to the audit tables, for reviewers or a SIEM. |

For phpMyAdmin or other tools, give each administrator their own MariaDB
account. Never use `root` through a web interface.

## Moving from SQLite to MariaDB

1. Install the requirements, then prepare MariaDB. The script installs
   `mariadb-server` if needed, enables the audit plugin, creates the
   database and accounts, applies migrations and sets grants. It is safe to
   re-run.

   ```bash
   sudo /opt/gns3-lab-portal/.venv/bin/pip install -r /opt/gns3-lab-portal/requirements.txt
   sudo bash /opt/gns3-lab-portal/scripts/setup-mariadb.sh
   ```

2. Stop the portal, then copy the data. The script backs up `audit.db`
   first, copies every table except sessions, verifies every row arrived,
   and can be re-run safely.

   ```bash
   sudo systemctl stop gns3-lab-portal
   sudo env PORTAL_DB_HOST=localhost \
     PORTAL_DB_MIGRATE_USER=gns3_portal_migrate \
     PORTAL_DB_MIGRATE_PASSWORD_FILE=/etc/gns3-lab-portal/db-migrate-password \
     /opt/gns3-lab-portal/.venv/bin/python /opt/gns3-lab-portal/scripts/migrate-sqlite-to-mariadb.py \
     --sqlite /opt/gns3-lab-portal/data/audit.db
   ```

3. Add the `PORTAL_DB_*` lines that `setup-mariadb.sh` printed to `.env`,
   then start the portal. Everyone signs in again, because sessions are not
   copied.

To go back, set `PORTAL_DB_BACKEND=sqlite`. The migration never modifies the
original `audit.db`.
