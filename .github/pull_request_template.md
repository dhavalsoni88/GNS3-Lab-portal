## What this changes

<!-- One or two sentences on what changed and why. Link the issue if there is one. -->

## How it was tested

- [ ] `PORTAL_DB_HOST=localhost PORTAL_DB_USER=root python -m pytest` (MariaDB)
- [ ] Checked in a browser, including phone width, if the interface changed

## Checklist

- [ ] New or changed routes check permissions and write an audit entry
- [ ] Schema changes add a new `database/mariadb/NNN_*.sql` (and a change-log trigger if the table holds access data)
- [ ] Docs in `docs/` and `CHANGELOG.md` are updated. To release, also bump `version` in `pyproject.toml` and date the changelog heading: merging then tags and publishes the release automatically
- [ ] No secrets, internal addresses or credentials in code, docs or tests
