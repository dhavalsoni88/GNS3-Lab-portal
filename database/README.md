# MariaDB database preparation

The schema establishes the v9.0 ownership and access-control model. It does
not migrate data by itself and should not be applied to the existing v8.x
SQLite-only portal until the v9.0 migration code is delivered and tested.

Use separate accounts:

- `gns3_portal_app`: routine runtime access to only the `gns3_lab_portal`
  database.
- `gns3_portal_migrate`: used only during schema migrations.
- Individual administrator accounts for phpMyAdmin; never use MariaDB `root`
  in the web interface.

The eventual installer will create exact grants. Do not use the placeholders in
this planning package as production credentials.
