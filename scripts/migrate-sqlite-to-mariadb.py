#!/usr/bin/env python3
"""Copy a v8.x SQLite portal database into MariaDB.

Stop the portal first. The script:

1. makes a consistent backup copy of the SQLite file next to it;
2. applies pending MariaDB schema migrations;
3. copies every table except sessions (users sign in again after the move);
4. verifies that every SQLite row is present in MariaDB.

It is safe to re-run: rows that already exist are left unchanged, so a
second run copies only rows added since the first. Change-log triggers record
the copied rows with the portal actor ``sqlite-migration``.

Connection settings come from the same PORTAL_DB_* variables as the portal;
set PORTAL_DB_MIGRATE_USER and PORTAL_DB_MIGRATE_PASSWORD_FILE to use the
migration account.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

os.environ["PORTAL_DB_BACKEND"] = "mariadb"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import portal_db  # noqa: E402

# Parent tables first; there are no foreign keys, but this keeps the change
# log readable. portal_sessions is intentionally excluded.
TABLES = (
    "portal_users", "lab_owners", "lab_shares", "backup_shares",
    "retained_backups", "lab_templates", "lab_template_access", "audit_log",
)
KEYS = {
    "portal_users": ("username",), "lab_owners": ("project_id",),
    "lab_shares": ("project_id", "username"), "backup_shares": ("project_id", "snapshot_id", "username"),
    "retained_backups": ("backup_id",), "lab_templates": ("template_id",),
    "lab_template_access": ("template_id", "subject_type", "subject_id"),
}


def sqlite_columns(source: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in source.execute(f"PRAGMA table_info({table})")]


def mariadb_columns(cursor, table: str) -> list[str]:
    cursor.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=DATABASE() AND table_name=%s", (table,))
    return [row[0] for row in cursor.fetchall()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sqlite", default=str(portal_db.SQLITE_PATH), help="path to the v8.x audit.db (default: %(default)s)")
    parser.add_argument("--dry-run", action="store_true", help="report what would be copied without writing to MariaDB")
    args = parser.parse_args()

    sqlite_path = Path(args.sqlite)
    if not sqlite_path.is_file():
        print(f"SQLite database not found: {sqlite_path}", file=sys.stderr)
        return 2
    source = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    if not args.dry_run:
        backup_path = sqlite_path.with_name(f"{sqlite_path.name}.pre-mariadb-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}")
        with sqlite3.connect(backup_path) as backup:
            source.backup(backup)
        print(f"SQLite backup written to {backup_path}")

    target = portal_db._migration_connection()
    try:
        if not args.dry_run:
            applied = portal_db.apply_mariadb_migrations(target)
            if applied:
                print("Applied schema migrations: " + ", ".join(applied))
        cursor = target.cursor()
        cursor.execute("SET @portal_actor='sqlite-migration', @portal_source_ip='', @portal_request_id=''")
        failures = 0
        for table in TABLES:
            if not source.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                print(f"{table:22} not present in SQLite, skipped")
                continue
            columns = [name for name in sqlite_columns(source, table) if name in set(mariadb_columns(cursor, table))]
            rows = source.execute(f"SELECT {', '.join(columns)} FROM {table}").fetchall()
            # Audit IDs are surrogate keys: a MariaDB row with the same ID but
            # different content is a collision, not an already-copied row.
            keys = ("id", "timestamp", "actor", "action") if table == "audit_log" else KEYS[table]
            key_index = [columns.index(key) for key in keys]
            copied = 0
            if not args.dry_run and rows:
                placeholders = ", ".join(["%s"] * len(columns))
                quoted = ", ".join(f"`{name}`" for name in columns)
                # INSERT IGNORE keeps re-runs idempotent and never overwrites
                # rows already changed in MariaDB.
                copied = cursor.executemany(f"INSERT IGNORE INTO `{table}` ({quoted}) VALUES ({placeholders})", rows) or 0
                target.commit()
            missing = 0
            if not args.dry_run:
                where = " AND ".join(f"`{key}`=%s" for key in keys)
                for row in rows:
                    cursor.execute(f"SELECT 1 FROM `{table}` WHERE {where}", tuple(row[i] for i in key_index))
                    if not cursor.fetchone():
                        missing += 1
            failures += missing
            status = "dry run" if args.dry_run else ("OK" if not missing else f"{missing} MISSING")
            print(f"{table:22} sqlite={len(rows):6} copied={copied:6} {status}")
        if args.dry_run:
            print("Dry run complete; MariaDB was not changed.")
        elif failures:
            print(f"Verification failed: {failures} rows are missing in MariaDB.", file=sys.stderr)
            return 1
        else:
            print("Migration verified. Set PORTAL_DB_BACKEND=mariadb in .env and restart the portal.")
        return 0
    finally:
        target.close()
        source.close()


if __name__ == "__main__":
    sys.exit(main())
