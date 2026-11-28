# Engineering guide

For engineers who change the portal. Read the [architecture](architecture.md)
first. Code conventions and the pull request steps are in
[CONTRIBUTING.md](../CONTRIBUTING.md).

## Code map

| File | What to look for |
|---|---|
| `app.py` | Settings at the top. `init_db` holds the start-up checks. The session and permission helpers are `signed_in_user`, `current_user`, `require_permission`, `require_project_access` and `require_lab_owner`. Then the GNS3 helpers (`gns3`, `stop_all_nodes`), the jobs (`run_backup_job`, `run_snapshot_import_job`, `start_import`), and the routes, grouped by page. |
| `portal_auth.py` | `ROLES` and `PERMISSIONS`, `user_permissions` (cached per request), `LdapDirectory`, the SAML helpers, `SecretBox`. |
| `portal_db.py` | `connect()` (commit or roll back, then close), `translate_for_mariadb`, migrations. Run `python3 portal_db.py status` or `python3 portal_db.py migrate`. |
| `database/mariadb/NNN_*.sql` | The schema, applied in order. Never edit a released file. |
| `scripts/upgrade.sh` | The one-step server upgrade. Change it whenever an upgrade needs a new manual step, so administrators never have to. |
| `scripts/migrate-sqlite-to-mariadb.py` | One-way import of pre-v9.0 SQLite data. Keep it working: older installs still need it. |
| `tests/` | `conftest.py` builds a fresh MariaDB database per run. `test_access.py` covers permissions and start-up checks, `test_identity.py` covers LDAP and SAML, and `test_database.py` covers audit and migration. |

## Set up a development machine

You need Python 3.10+, `pkg-config libxmlsec1-dev` (for `xmlsec`) and a
throwaway MariaDB 10.6+. Docker is the quickest way to get one:

```bash
docker run -d --name portal-db -e MARIADB_ROOT_PASSWORD=dev -p 3306:3306 mariadb:10.11
```

Or install `mariadb-server` and use the local socket (`PORTAL_DB_HOST=localhost`).

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
PORTAL_DB_HOST=127.0.0.1 PORTAL_DB_USER=root PORTAL_DB_PASSWORD=dev python -m pytest -q
```

The tests drop and recreate the `gns3_portal_test` database. They never need
a GNS3 server: tests swap `app.state.gns3_client` for an `httpx.MockTransport`
stub (see `gns3_stub` in `tests/test_access.py`).

To run the portal itself, point a copy of `.env.example` at a **test** GNS3
controller and your local MariaDB, and set `PORTAL_DB_AUTO_MIGRATE=true` so
the schema is created for you:

```bash
cp .env.example .env    # edit GNS3_URL, PORTAL_SECRET_KEY and the PORTAL_DB_* lines
set -a; . ./.env; set +a
uvicorn app:app --reload --port 8088
```

## Making common changes

### A new route that changes something

1. Check the permission first: `require_permission(request, "labs.edit")`.
   Use `portal_wide=True` for administration, so it is refused while an
   admin is viewing as another user.
2. For a lab, call `require_project_access` (unknown labs answer 404, so lab
   IDs do not leak). Add `require_lab_owner` for destructive actions.
3. Refuse changes to a lab while `backup_is_active(project_id)`.
4. Validate every form field at the top: length, allowed values, and paths
   resolved inside their expected folder.
5. Record the action with `audit(audit_actor(request), "action_name", target, {...})`.
   Never put a password or token in the details.
6. Add a test that a user without access is refused.

### A schema change

1. Add `database/mariadb/NNN_description.sql` with the next number. Use
   `CREATE TABLE IF NOT EXISTS` and `ADD COLUMN IF NOT EXISTS`, so a re-run is
   harmless.
2. If the table holds access data (users, roles, owners, shares), add
   change-log triggers like those in `002_audit.sql` and `003_identity.sql`.
3. A new table needs grants: `scripts/setup-mariadb.sh` gives the runtime
   account read and write on every non-audit table when re-run.
4. If the data also existed in pre-v9.0 SQLite installs, add the table and
   its key columns to `TABLES` and `KEYS` in the migration script.

Tests apply migrations automatically. Production applies them with
`setup-mariadb.sh` or `portal_db.py migrate`, and the portal refuses to start
until they are applied.

### SQL style

Write `?` placeholders and, for upserts,
`INSERT … ON CONFLICT(key) DO UPDATE SET col=excluded.col`. `portal_db`
rewrites them for MariaDB. Tables use the case-insensitive
`utf8mb4_unicode_ci` collation, so `alice` and `Alice` are the same username.
Keep each `db.connect()` block to one unit of work: everything in it commits
together, or nothing does.

### Calling GNS3

Use `await gns3(method, path, ...)`. It logs to `gns3-api.log`, hides the
GNS3 password and turns controller errors into `HTTPException`. Long
transfers (exports, imports) open their own client with streaming, as
`stream_project_export` does.

## Known limits

Run `grep -rn "shortcut:" *.py` to list deliberate shortcuts and when to
revisit them. The main one: sessions, jobs, SAML requests and sign-in limits
are kept in process memory, so the portal runs as a single uvicorn worker.
Move them to MariaDB before building the cluster phase.

## Rehearsing an upgrade

`scripts/upgrade.sh` stops the service and rewrites `.env`, so rehearse it
before changing it. CI only runs `shellcheck`; the full run needs systemd and
a clean server. In a throwaway VM or container: check out the previous
release in `/opt/gns3-lab-portal`, create data with it (a SQLite database
for a v8.x rehearsal), `git pull` the new code, run `scripts/upgrade.sh`,
then run it a second time (it must be a quiet routine update) and sign in.

## Releasing

1. Move the **Unreleased** notes in `CHANGELOG.md` under a dated version heading,
   including any steps administrators must take.
2. Update `version` in `pyproject.toml`.
3. CI must be green. It installs the requirements, checks syntax and the
   shell scripts, and runs the tests on MariaDB.
4. If the release adds a migration, say so in [upgrading.md](upgrading.md).
