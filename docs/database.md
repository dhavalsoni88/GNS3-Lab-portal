# Database

The portal stores accounts, roles, sessions, lab ownership, sharing,
templates, identity providers and the audit log in **MariaDB 10.6 or newer**.
It is the only supported database since v9.0. Earlier SQLite installs move
across with a verified, re-runnable script: see
[Moving from SQLite to MariaDB](upgrading.md#moving-from-sqlite-to-mariadb).

[`portal_db.py`](../portal_db.py) opens one short connection per unit of
work, commits on success and rolls back on any error.

## Schema and migrations

| Migration | Contents |
|---|---|
| [`001_core.sql`](../database/mariadb/001_core.sql) | Core tables: users, sessions, owners, shares, retained backups, templates, audit log. |
| [`002_audit.sql`](../database/mariadb/002_audit.sql) | Audit request context, append-only triggers and the change history. |
| [`003_identity.sql`](../database/mariadb/003_identity.sql) | Sign-in sources, roles, identity providers and group mappings. |

Migrations are applied once each, in order, and recorded in
`schema_migrations`:

```bash
python3 portal_db.py status    # list pending migrations
python3 portal_db.py migrate   # apply them (uses the migration account)
```

The portal refuses to start while MariaDB migrations are pending.

## Audit

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

## Backing up the database

Back up MariaDB with the rest of the server, at least daily:

```bash
sudo mariadb-dump --single-transaction --routines --triggers gns3_lab_portal | gzip > /backup/gns3_lab_portal-$(date +%F).sql.gz
```

`--triggers` matters: the append-only and change-history triggers are part
of the audit design. Test a restore on a spare server now and then.
