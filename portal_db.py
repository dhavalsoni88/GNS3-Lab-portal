"""Database access for the GNS3 Lab Portal (MariaDB only).

SQLite support ended in v9.0; scripts/migrate-sqlite-to-mariadb.py moves an
existing SQLite database across (see docs/upgrading.md). Application queries
still use ``?`` placeholders and ``ON CONFLICT ... DO UPDATE`` upserts, which
this module translates to MariaDB SQL.
"""
from __future__ import annotations

import contextvars
import os
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

BASE_DIR = Path(__file__).resolve().parent
# An existing v8.x/v9-preview .env may still say sqlite: stop rather than start empty.
if os.getenv("PORTAL_DB_BACKEND", "mariadb").strip().lower() != "mariadb":
    raise RuntimeError("SQLite is no longer supported. Move the data with scripts/migrate-sqlite-to-mariadb.py "
                       "(see docs/upgrading.md), then set PORTAL_DB_BACKEND=mariadb or remove the line.")
# Where a pre-v9.0 SQLite database lives; the portal will not start on an empty MariaDB while it exists.
LEGACY_SQLITE_PATH = Path(os.getenv("AUDIT_DB", str(BASE_DIR / "data" / "audit.db")))
MARIADB_HOST = os.getenv("PORTAL_DB_HOST", "127.0.0.1")
MARIADB_PORT = int(os.getenv("PORTAL_DB_PORT", "3306"))
MARIADB_NAME = os.getenv("PORTAL_DB_NAME", "gns3_lab_portal")
MARIADB_USER = os.getenv("PORTAL_DB_USER", "gns3_portal_app")
# Prefer a root-owned secret file over a password in the environment so the
# credential is not visible through /proc/<pid>/environ or service listings.
MARIADB_PASSWORD_FILE = os.getenv("PORTAL_DB_PASSWORD_FILE", "")
MARIADB_SSL_CA = os.getenv("PORTAL_DB_SSL_CA", "")
# Like the mariadb client, host "localhost" means the local Unix socket.
MARIADB_SOCKET = os.getenv("PORTAL_DB_SOCKET", "/run/mysqld/mysqld.sock")
MARIADB_MIGRATIONS_DIR = BASE_DIR / "database" / "mariadb"

# Per-request context recorded with every audit event and, on MariaDB, made
# available to the change-log triggers as session variables.
request_context: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar("portal_request_context")


def current_context() -> dict[str, str]:
    """Return this request's audit context, or an empty throwaway dict outside a request."""
    context = request_context.get(None)
    return {} if context is None else context


def _mariadb_password() -> str:
    if MARIADB_PASSWORD_FILE:
        return Path(MARIADB_PASSWORD_FILE).read_text(encoding="utf-8").strip()
    return os.getenv("PORTAL_DB_PASSWORD", "")


def _integrity_errors() -> tuple[type[BaseException], ...]:
    import pymysql
    return (pymysql.err.IntegrityError,)


IntegrityError = _integrity_errors()


class Row(dict):
    """A result row usable both as ``row["name"]`` and ``row[0]``."""

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


_UPSERT = re.compile(r"ON CONFLICT\s*\([^)]*\)\s*DO UPDATE SET", re.IGNORECASE)
_EXCLUDED = re.compile(r"\bexcluded\.(\w+)", re.IGNORECASE)
_NOCASE = re.compile(r"\s+COLLATE\s+NOCASE", re.IGNORECASE)


def translate_for_mariadb(sql: str) -> str:
    """Convert the portal's SQLite dialect into MariaDB SQL.

    MariaDB tables use the case-insensitive utf8mb4_unicode_ci collation, so
    ``COLLATE NOCASE`` is simply removed.
    """
    sql = _NOCASE.sub("", sql)
    if _UPSERT.search(sql):
        sql = _UPSERT.sub("ON DUPLICATE KEY UPDATE", sql)
        sql = _EXCLUDED.sub(r"VALUES(\1)", sql)
    return sql.replace("%", "%%").replace("?", "%s")


class _Cursor:
    def __init__(self, cursor: Any, columns: Sequence[str] | None):
        self._cursor = cursor
        self._columns = columns

    def _row(self, values: Any) -> Row | None:
        if values is None:
            return None
        if isinstance(values, dict):
            return Row(values)
        return Row(zip(self._columns or [str(i) for i in range(len(values))], values))

    def fetchone(self) -> Row | None:
        return self._row(self._cursor.fetchone())

    def fetchall(self) -> list[Row]:
        return [self._row(values) for values in self._cursor.fetchall()]  # type: ignore[misc]

    def __iter__(self) -> Iterator[Row]:
        return iter(self.fetchall())

    @property
    def rowcount(self) -> int:
        return int(self._cursor.rowcount)


class Connection:
    """A small DB-API wrapper with a ``connection.execute(sql, params)`` shape."""

    def __init__(self, raw: Any):
        self.raw = raw

    def execute(self, sql: str, params: Sequence[Any] = ()) -> _Cursor:
        cursor = self.raw.cursor()
        cursor.execute(translate_for_mariadb(sql), tuple(params))
        columns = [item[0] for item in cursor.description] if cursor.description else None
        return _Cursor(cursor, columns)



def split_sql(script: str) -> list[str]:
    """Split a migration file into statements, honouring ``DELIMITER`` blocks."""
    statements, buffer, delimiter = [], [], ";"
    for line in script.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("DELIMITER "):
            delimiter = stripped.split(None, 1)[1]
            continue
        if not buffer and (not stripped or stripped.startswith("--")):
            continue
        buffer.append(line)
        if stripped.endswith(delimiter):
            statement = "\n".join(buffer).rstrip()
            statements.append(statement[: -len(delimiter)].strip())
            buffer = []
    if "".join(buffer).strip():
        statements.append("\n".join(buffer).strip())
    return [item for item in statements if item]


def _connect_mariadb(database: str | None = MARIADB_NAME, user: str | None = None, password: str | None = None) -> Any:
    try:
        import pymysql
    except ImportError as exc:  # pragma: no cover - deployment error
        raise RuntimeError("PORTAL_DB_BACKEND=mariadb requires the PyMySQL package.") from exc
    ssl = {"ca": MARIADB_SSL_CA} if MARIADB_SSL_CA else None
    location = {"unix_socket": MARIADB_SOCKET} if MARIADB_HOST == "localhost" else {"host": MARIADB_HOST, "port": MARIADB_PORT}
    return pymysql.connect(
        **location, user=user or MARIADB_USER,
        password=_mariadb_password() if password is None else password,
        database=database, charset="utf8mb4", autocommit=False, ssl=ssl,
    )


@contextmanager
def connect() -> Iterator[Connection]:
    """Open a connection, commit on success, roll back on error, then close."""
    raw = _connect_mariadb()
    connection = Connection(raw)
    try:
        context = current_context()
        # Read by the change-log triggers in 002_audit.sql.
        connection.execute("SET @portal_actor=?, @portal_source_ip=?, @portal_request_id=?", (
            context.get("actor", "")[:255], context.get("source_ip", "")[:45], context.get("request_id", "")[:64],
        ))
        yield connection
        raw.commit()
    except BaseException:
        raw.rollback()
        raise
    finally:
        raw.close()


def apply_mariadb_migrations(raw_connection: Any | None = None) -> list[str]:
    """Apply pending ``database/mariadb/NNN_*.sql`` files in order.

    Each file is applied once and recorded in ``schema_migrations``. The
    application account normally lacks DDL rights in production; in that case
    the installer runs this with the migration account instead.
    """
    owns_connection = raw_connection is None
    raw = raw_connection or _connect_mariadb()
    applied: list[str] = []
    try:
        cursor = raw.cursor()
        cursor.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
            version VARCHAR(64) NOT NULL PRIMARY KEY, applied_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""")
        cursor.execute("SELECT version FROM schema_migrations")
        done = {row[0] for row in cursor.fetchall()}
        for path in sorted(MARIADB_MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql")):
            version = path.stem
            if version in done:
                continue
            for statement in split_sql(path.read_text(encoding="utf-8")):
                cursor.execute(statement)
            cursor.execute("INSERT INTO schema_migrations(version) VALUES(%s)", (version,))
            raw.commit()
            applied.append(version)
    finally:
        if owns_connection:
            raw.close()
    return applied


def pending_mariadb_migrations() -> list[str]:
    with connect() as connection:
        try:
            done = {str(row[0]) for row in connection.execute("SELECT version FROM schema_migrations").fetchall()}
        except Exception:
            done = set()
    return [path.stem for path in sorted(MARIADB_MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql")) if path.stem not in done]


def _migration_connection() -> Any:
    """Connect with the DDL-capable migration account when one is configured."""
    user = os.getenv("PORTAL_DB_MIGRATE_USER", "")
    password_file = os.getenv("PORTAL_DB_MIGRATE_PASSWORD_FILE", "")
    password = Path(password_file).read_text(encoding="utf-8").strip() if password_file else None
    return _connect_mariadb(user=user or None, password=password)


if __name__ == "__main__":
    import sys

    command = sys.argv[1] if len(sys.argv) > 1 else "status"
    if command == "migrate":
        migration_connection = _migration_connection()
        try:
            applied = apply_mariadb_migrations(migration_connection)
        finally:
            migration_connection.close()
        print("Applied: " + ", ".join(applied) if applied else "Schema is up to date.")
    elif command == "status":
        pending = pending_mariadb_migrations()
        print("Pending: " + ", ".join(pending) if pending else "Schema is up to date.")
        sys.exit(1 if pending else 0)
    else:
        sys.exit("Usage: python3 portal_db.py [status|migrate]")
