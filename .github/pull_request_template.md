## What this changes

<!-- One or two sentences on what changed and why. Link the issue if there is one. -->

## How it was tested

- [ ] `PORTAL_DB_HOST=localhost PORTAL_DB_USER=root python -m pytest` (MariaDB)
- [ ] Checked in a browser, including phone width, if the interface changed

## Checklist

- [ ] New or changed routes check permissions and write an audit entry
- [ ] Schema changes add a new `database/mariadb/NNN_*.sql` (and a change-log trigger if the table holds access data)
- [ ] Docs in `docs/` and the **Unreleased** section of `CHANGELOG.md` are updated
- [ ] No secrets, internal addresses or credentials in code, docs or tests
