# MariaDB database

The portal runs on SQLite (default) or MariaDB. Multi-server clusters require
MariaDB.

- `mariadb/001_core.sql` holds the core tables. They match the v8.x SQLite
  tables one-to-one.
- `mariadb/002_audit.sql` adds the audit layer:
  - `audit_log` portal events gain source IP, user agent, request ID,
    outcome, and the connecting MariaDB account.
  - `db_change_log` records the old and new values of every INSERT, UPDATE
    and DELETE on users, lab ownership, shares, retained backups and
    templates. Each entry records the portal user, source IP and request ID.
    Password hashes are never copied.
  - Both tables are append-only: triggers reject UPDATE and DELETE for every
    account, including root.

Migrations are applied in order and recorded in `schema_migrations`. Use
`python3 portal_db.py status` to list pending migrations and
`python3 portal_db.py migrate` to apply them.

## Accounts

`scripts/setup-mariadb.sh` creates these accounts. It stores their generated
passwords in `/etc/gns3-lab-portal/` with mode 0640.

| Account | Rights |
|---|---|
| `gns3_portal_app` | Portal runtime. SELECT/INSERT/UPDATE/DELETE on lab tables; SELECT/INSERT only on `audit_log` and `db_change_log`. No DDL. |
| `gns3_portal_migrate` | Schema migrations only. Owns the triggers. |
| `gns3_portal_auditor` | Read-only access to `audit_log` and `db_change_log`, for reviewers or a SIEM. |

The script also enables the MariaDB `server_audit` plugin. It writes
connections, failed logins, schema changes and grant changes for every
account to `/var/log/mysql/server_audit.log`, with passwords masked. Purging
old audit rows needs a DBA to drop an append-only trigger first, and that
drop is itself recorded in this log.

For phpMyAdmin, use individual administrator accounts. Never sign in as
MariaDB `root` through a web interface.
