## What this changes

<!-- One or two sentences on what changed and why. Link the issue if there is one. -->

## How it was tested

- [ ] `python -m pytest` (SQLite)
- [ ] `PORTAL_TEST_MARIADB=1 … python -m pytest` (MariaDB)
- [ ] Checked in a browser, including phone width, if the interface changed

## Checklist

- [ ] New or changed routes check permissions and write an audit entry
- [ ] Schema changes add a new `database/mariadb/NNN_*.sql` and update the SQLite schema
- [ ] Docs in `docs/` and the **Unreleased** section of `CHANGELOG.md` are updated
- [ ] No secrets, internal addresses or credentials in code, docs or tests
