# Contributing

Thanks for helping improve the GNS3 Lab Portal.

## Set up

```bash
git clone https://github.com/dhavalsoni88/GNS3-Lab-portal.git
cd GNS3-Lab-portal
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
```

On Debian or Ubuntu, `pip` may need `sudo apt install pkg-config libxmlsec1-dev`
to build `xmlsec`.

## Run the tests

The suite runs without a GNS3 server; tests that need one use a stub. It
needs a MariaDB server. Each run drops and recreates a `gns3_portal_test`
database, so point it at a disposable server:

```bash
PORTAL_DB_HOST=localhost PORTAL_DB_USER=root python -m pytest
```

CI runs it on every pull request. The [engineering guide](docs/engineering.md)
explains the code layout and how to start a throwaway MariaDB.

## Run the portal locally

```bash
cp .env.example .env    # set GNS3_URL to a test controller, never production
set -a; . ./.env; set +a
uvicorn app:app --reload --port 8088
```

Use a test GNS3 controller. Never point a development portal at a production
GNS3 server or add one as a compute.

## Code conventions

- **Layout:** `app.py` holds the routes, `portal_db.py` the database layer
  and `portal_auth.py` roles and identity providers. Templates are Jinja2;
  styles are in `static/app.css` and use its CSS variables.
- **SQL:** MariaDB is the only database. Queries use `?` placeholders and
  `ON CONFLICT … DO UPDATE` upserts, which `portal_db` translates; keep to
  that style so all queries read the same.
- **Schema changes:** add a new numbered file in `database/mariadb/`, never
  edit one already released, and add change-log triggers for tables that
  hold access-control data.
- **Authorisation:** every new route that changes something checks a
  permission with `require_permission` and, for destructive lab actions,
  `require_lab_owner`. Record the action with `audit(...)`.
- **Secrets:** never log passwords, tokens or secrets, or put them in audit
  details or error messages.
- **Accessibility:** use real buttons, links and labels; keep text contrast
  at 4.5:1; make sure new tables work at phone width (`table.data.responsive`
  with `data-label` cells).

## Pull requests

1. Branch from `main` and keep each pull request to one change.
2. Add or update tests, and run the test suite.
3. Update the docs in `docs/` and add an entry under **Unreleased** in
   `CHANGELOG.md`.
4. Fill in the pull request template.
