"""Small, server-side GNS3 lab and snapshot portal."""
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import shutil
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from time import perf_counter
from typing import Any
from urllib.parse import quote, quote_plus, urlsplit

import httpx
from fastapi import File, FastAPI, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

import portal_auth as auth
import portal_db as db

BASE_DIR = Path(__file__).resolve().parent
GNS3_URL = os.getenv("GNS3_URL", "http://192.168.10.10:3080").rstrip("/")
# Public address where users load the GNS3 WebUI. Keep this separate from the
# API address: the portal may use 127.0.0.1 while users reach a reverse proxy.
GNS3_WEBUI_URL = os.getenv("GNS3_WEBUI_URL", GNS3_URL).rstrip("/")
# The bundled WebUI on this controller uses its legacy Angular routes. The
# server ID is normally 1 for the local controller, but remains configurable.
GNS3_WEBUI_SERVER_ID = os.getenv("GNS3_WEBUI_SERVER_ID", "1").strip()
GNS3_USERNAME = os.getenv("GNS3_USERNAME", "")
GNS3_PASSWORD = os.getenv("GNS3_PASSWORD", "")
PORTAL_USERNAME = os.getenv("PORTAL_USERNAME", "portaladmin")
PORTAL_PASSWORD = os.getenv("PORTAL_PASSWORD", "change-this-now")
SECRET_KEY = os.getenv("PORTAL_SECRET_KEY", "change-this-before-production")
# Public https:// address users and identity providers use to reach the portal.
# Required for SAML: it forms the SP entity ID and the assertion consumer URL.
PORTAL_PUBLIC_URL = os.getenv("PORTAL_PUBLIC_URL", "").strip().rstrip("/")
AUTH_ENABLED = os.getenv("PORTAL_AUTH_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
SESSION_IDLE_SECONDS = max(60, int(os.getenv("PORTAL_SESSION_IDLE_SECONDS", "1800")))
SESSION_MAX_SECONDS = max(SESSION_IDLE_SECONDS, int(os.getenv("PORTAL_SESSION_MAX_SECONDS", "28800")))
PORTAL_DB_AUTO_MIGRATE = os.getenv("PORTAL_DB_AUTO_MIGRATE", "false").strip().lower() in {"1", "true", "yes", "on"}
# Reverse proxies whose X-Forwarded-For header is trusted for audit source IPs.
PORTAL_TRUSTED_PROXIES = frozenset(item.strip() for item in os.getenv("PORTAL_TRUSTED_PROXIES", "127.0.0.1,::1").split(",") if item.strip())
UPLOAD_DIR = Path(os.getenv("PORTAL_UPLOAD_DIR", str(BASE_DIR / "data" / "uploads")))
USER_AVATAR_DIR = Path(os.getenv("PORTAL_AVATAR_DIR", str(BASE_DIR / "data" / "avatars"))).expanduser()
MAX_AVATAR_BYTES = 2 * 1024 * 1024
RETAINED_BACKUP_DIR = Path(os.getenv("RETAINED_BACKUP_DIR", "/mnt/HDD_STORE/my-lab-retained-backups")).expanduser()
LAB_TEMPLATE_DIR = Path(os.getenv("LAB_TEMPLATE_DIR", "/mnt/HDD_STORE/my-lab-templates")).expanduser()
# Optional local root used to report native snapshot storage sizes. The GNS3
# API provides snapshot metadata but does not include filesystem usage.
GNS3_PROJECTS_DIR_VALUE = os.getenv("GNS3_PROJECTS_DIR", "").strip()
GNS3_PROJECTS_DIR = Path(GNS3_PROJECTS_DIR_VALUE).expanduser() if GNS3_PROJECTS_DIR_VALUE else None
# When project ``snapshots`` directories are symlinked to a dedicated mounted
# disk, this value allows the portal to safely follow only that storage root.
GNS3_SNAPSHOTS_DIR_VALUE = os.getenv("GNS3_SNAPSHOTS_DIR", "").strip()
GNS3_SNAPSHOTS_DIR = Path(GNS3_SNAPSHOTS_DIR_VALUE).expanduser() if GNS3_SNAPSHOTS_DIR_VALUE else None
# Native snapshots can take many minutes for large labs. Browsers poll a job
# status endpoint, so this timeout only governs portal-to-controller traffic.
GNS3_TIMEOUT_SECONDS = float(os.getenv("GNS3_TIMEOUT_SECONDS", "3600"))
# GNS3's snapshot API is synchronous and does not publish byte-level progress.
# This controls the conservative UI estimate while the controller creates the
# native snapshot; it does not affect the controller request timeout.
SNAPSHOT_ESTIMATED_SECONDS = max(60.0, float(os.getenv("SNAPSHOT_ESTIMATED_SECONDS", "3600")))
# Keep portal/application and controller-API diagnostics together, but separate
# them into files so a busy controller trace cannot hide an application error.
# The service account must be able to create and write this directory.
PORTAL_LOG_DIR = Path(os.getenv("PORTAL_LOG_DIR", str(BASE_DIR / "logs"))).expanduser()
PORTAL_LOG_RETENTION_DAYS = max(1, int(os.getenv("PORTAL_LOG_RETENTION_DAYS", "14")))
PORTAL_LOG_LEVEL = os.getenv("PORTAL_LOG_LEVEL", "INFO").upper()
PORTAL_MONITOR_DISKS = tuple(name for name in (item.strip() for item in os.getenv("PORTAL_MONITOR_DISKS", "sda,sdb").split(",")) if name and name.replace("-", "").replace("_", "").isalnum())
GNS3_MONITOR_COMPUTE_ID = os.getenv("GNS3_MONITOR_COMPUTE_ID", "").strip()
# argon2-cffi defaults follow RFC 9106's second recommended (memory-constrained) profile.
password_hasher = PasswordHasher()
secret_box = auth.SecretBox(SECRET_KEY)
portal_logger = logging.getLogger("gns3_lab_portal")
api_logger = logging.getLogger("gns3_lab_portal.gns3_api")


def format_storage_size(size_bytes: int | float | None) -> str:
    """Render byte counts in the most useful binary storage unit."""
    if size_bytes is None:
        return "Not matched"
    value = float(size_bytes)
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    index = 0
    while value >= 1024 and index < len(units) - 1:
        value /= 1024
        index += 1
    return f"{int(value)} {units[index]}" if index == 0 else f"{value:.2f} {units[index]}"


def numeric_value(data: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return None


def disk_io_counters(device_names: tuple[str, ...]) -> dict[str, dict[str, int]]:
    counters: dict[str, dict[str, int]] = {}
    try:
        lines = Path("/proc/diskstats").read_text(encoding="utf-8").splitlines()
    except OSError:
        return counters
    for line in lines:
        fields = line.split()
        if len(fields) < 10 or fields[2] not in device_names:
            continue
        try:
            counters[fields[2]] = {"read_bytes": int(fields[5]) * 512, "write_bytes": int(fields[9]) * 512}
        except ValueError:
            continue
    return counters


def gns3_webui_project_url(project_id: str) -> str:
    """Return the direct project route for the bundled GNS3 WebUI.

    This controller lists projects at /static/web-ui/server/1/projects; the
    corresponding project workspace route is /static/web-ui/server/1/project/
    <project-id>.
    """
    server_id = quote(GNS3_WEBUI_SERVER_ID or "1", safe="")
    return (
        f"{GNS3_WEBUI_URL}/static/web-ui/server/{server_id}/project/"
        f"{quote(project_id, safe='')}"
    )


def configure_logging() -> None:
    """Write safe, rotating diagnostics to one administrator-configured folder."""
    if getattr(configure_logging, "configured", False):
        return
    PORTAL_LOG_DIR.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)sZ %(levelname)s %(name)s %(message)s", "%Y-%m-%dT%H:%M:%S")
    for logger, filename in ((portal_logger, "portal.log"), (api_logger, "gns3-api.log")):
        logger.setLevel(getattr(logging, PORTAL_LOG_LEVEL, logging.INFO))
        logger.propagate = False
        handler = TimedRotatingFileHandler(
            PORTAL_LOG_DIR / filename, when="midnight", backupCount=PORTAL_LOG_RETENTION_DAYS, encoding="utf-8", utc=True,
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    configure_logging.configured = True
    portal_logger.info("logging_started log_dir=%s retention_days=%s", PORTAL_LOG_DIR, PORTAL_LOG_RETENTION_DAYS)


def safe_api_detail(value: Any, limit: int = 500) -> str:
    """Keep useful controller errors but never log credentials or huge bodies."""
    text = json.dumps(value, ensure_ascii=False, default=str) if isinstance(value, (dict, list)) else str(value)
    text = text.replace(GNS3_PASSWORD, "[REDACTED]") if GNS3_PASSWORD else text
    return text[:limit].replace("\n", " ")


def avatar_format(data: bytes) -> tuple[str, str] | None:
    """Accept only common raster-image signatures; SVG and arbitrary files are excluded."""
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg", "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png", "image/png"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp", "image/webp"
    return None


def safe_avatar_path(filename: str | None) -> Path | None:
    """Resolve only a generated avatar file directly below the avatar directory."""
    if not filename or Path(filename).name != filename:
        return None
    try:
        path = (USER_AVATAR_DIR / filename).resolve(strict=True)
        path.relative_to(USER_AVATAR_DIR.resolve(strict=True))
    except (OSError, ValueError):
        return None
    return path if path.is_file() else None


def init_db() -> None:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    USER_AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    RETAINED_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    LAB_TEMPLATE_DIR.mkdir(parents=True, exist_ok=True)
    if db.BACKEND == "mariadb":
        # In production the application account has no DDL rights; the
        # installer applies migrations with the separate migration account.
        if PORTAL_DB_AUTO_MIGRATE:
            db.apply_mariadb_migrations()
        pending = db.pending_mariadb_migrations()
        if pending:
            raise RuntimeError(f"MariaDB schema migrations are pending: {', '.join(pending)}. Run: python3 portal_db.py migrate")
    with db.connect() as connection:
        if db.BACKEND == "sqlite":
            init_sqlite_schema(connection)
        connection.execute("DELETE FROM portal_sessions WHERE expires_at < ?", (datetime.now(timezone.utc).isoformat(),))
        # Preserve the prior deployment path: the existing environment login
        # becomes the first local administrator on the first RBAC startup.
        existing = connection.execute("SELECT 1 FROM portal_users LIMIT 1").fetchone()
        if not existing:
            connection.execute(
                "INSERT INTO portal_users(username,password_hash,role,enabled,created_at) VALUES(?,?,?,?,?)",
                (PORTAL_USERNAME, password_hash(PORTAL_PASSWORD), "admin", 1, datetime.now(timezone.utc).isoformat()),
            )
    auth.seed_roles_from_legacy()


def init_sqlite_schema(connection: db.Connection) -> None:
    """Create or upgrade the SQLite schema. MariaDB uses database/mariadb/*.sql."""
    connection.execute("""CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL, actor TEXT NOT NULL,
        action TEXT NOT NULL, target TEXT NOT NULL, details TEXT
    )""")
    audit_columns = {row[1] for row in connection.execute("PRAGMA table_info(audit_log)")}
    for name, column_type in (("source_ip", "TEXT"), ("user_agent", "TEXT"), ("request_id", "TEXT"), ("outcome", "TEXT NOT NULL DEFAULT 'success'")):
        if name not in audit_columns:
            connection.execute(f"ALTER TABLE audit_log ADD COLUMN {name} {column_type}")
    connection.execute("""CREATE TABLE IF NOT EXISTS portal_users (
        username TEXT PRIMARY KEY COLLATE NOCASE, password_hash TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('admin', 'user')), enabled INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS portal_sessions (
        session_id_hash TEXT PRIMARY KEY, username TEXT NOT NULL COLLATE NOCASE,
        issued_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, expires_at TEXT NOT NULL,
        server_epoch TEXT NOT NULL
    )""")
    connection.execute("CREATE INDEX IF NOT EXISTS portal_sessions_username ON portal_sessions(username)")
    user_columns = {row[1] for row in connection.execute("PRAGMA table_info(portal_users)")}
    for name in ("email", "first_name", "last_name", "avatar_filename", "retained_backup_limit"):
        if name not in user_columns:
            column_type = "INTEGER NOT NULL DEFAULT 2" if name == "retained_backup_limit" else "TEXT"
            connection.execute(f"ALTER TABLE portal_users ADD COLUMN {name} {column_type}")
    connection.execute("""CREATE TABLE IF NOT EXISTS lab_owners (
        project_id TEXT PRIMARY KEY, username TEXT NOT NULL COLLATE NOCASE,
        assigned_at TEXT NOT NULL
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS retained_backups (
        backup_id TEXT PRIMARY KEY, owner TEXT NOT NULL COLLATE NOCASE,
        source_project_id TEXT NOT NULL, source_lab_name TEXT NOT NULL,
        backup_name TEXT NOT NULL, archive_path TEXT NOT NULL UNIQUE,
        size_bytes INTEGER NOT NULL, retained_at TEXT NOT NULL
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS lab_shares (
        project_id TEXT NOT NULL, username TEXT NOT NULL COLLATE NOCASE,
        shared_by TEXT NOT NULL COLLATE NOCASE, shared_at TEXT NOT NULL,
        PRIMARY KEY(project_id, username)
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS backup_shares (
        project_id TEXT NOT NULL, snapshot_id TEXT NOT NULL, username TEXT NOT NULL COLLATE NOCASE,
        shared_by TEXT NOT NULL COLLATE NOCASE, shared_at TEXT NOT NULL,
        PRIMARY KEY(project_id, snapshot_id, username)
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS lab_templates (
        template_id TEXT PRIMARY KEY, owner TEXT NOT NULL COLLATE NOCASE,
        source_project_id TEXT NOT NULL, snapshot_id TEXT NOT NULL,
        name TEXT NOT NULL, description TEXT, archive_path TEXT, size_bytes INTEGER,
        created_at TEXT NOT NULL,
        UNIQUE(source_project_id, snapshot_id)
    )""")
    template_columns = {row[1] for row in connection.execute("PRAGMA table_info(lab_templates)")}
    for name, column_type in (("archive_path", "TEXT"), ("size_bytes", "INTEGER"), ("approval_status", "TEXT NOT NULL DEFAULT 'approved'"), ("approved_by", "TEXT"), ("approved_at", "TEXT")):
        if name not in template_columns:
            connection.execute(f"ALTER TABLE lab_templates ADD COLUMN {name} {column_type}")
    for name, column_type in (("auth_source", "TEXT NOT NULL DEFAULT 'local'"), ("auth_provider", "TEXT"), ("external_id", "TEXT"), ("last_login_at", "TEXT")):
        if name not in {row[1] for row in connection.execute("PRAGMA table_info(portal_users)")}:
            connection.execute(f"ALTER TABLE portal_users ADD COLUMN {name} {column_type}")
    connection.execute("""CREATE TABLE IF NOT EXISTS user_roles (
        username TEXT NOT NULL COLLATE NOCASE, role_key TEXT NOT NULL, source TEXT NOT NULL,
        assigned_at TEXT NOT NULL, PRIMARY KEY(username, role_key, source)
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS identity_providers (
        provider_id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('ldap','saml')), name TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 0, config TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS group_role_mappings (
        provider_id TEXT NOT NULL, group_id TEXT NOT NULL COLLATE NOCASE, group_name TEXT NOT NULL,
        role_key TEXT NOT NULL, created_by TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(provider_id, group_id, role_key)
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS lab_template_access (
        template_id TEXT NOT NULL, subject_type TEXT NOT NULL CHECK(subject_type IN ('local_user','directory_group')),
        subject_id TEXT NOT NULL COLLATE NOCASE, granted_by TEXT NOT NULL COLLATE NOCASE,
        granted_at TEXT NOT NULL, PRIMARY KEY(template_id, subject_type, subject_id)
    )""")


def audit(actor: str, action: str, target: str, details: dict[str, Any] | None = None, outcome: str = "success") -> None:
    context = db.current_context()
    with db.connect() as connection:
        connection.execute(
            "INSERT INTO audit_log(timestamp, actor, action, target, details, source_ip, user_agent, request_id, outcome) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), actor[:255], action, target[:512], json.dumps(details or {}),
             context.get("source_ip") or None, (context.get("user_agent") or "")[:512] or None, context.get("request_id") or None, outcome),
        )


def recent_audit(limit: int = 20, username: str | None = None, include_all: bool = False) -> list[dict[str, str]]:
    with db.connect() as connection:
        if include_all or not username:
            rows = connection.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        else:
            rows = connection.execute("SELECT * FROM audit_log WHERE actor=? OR actor LIKE ? ORDER BY id DESC LIMIT ?", (username, f"% impersonating {username}", limit)).fetchall()
    return [dict(row) for row in rows]


def password_hash(password: str) -> str:
    return password_hasher.hash(password)


def password_needs_rehash(stored: str) -> bool:
    """v8.x scrypt hashes, and Argon2id hashes with old parameters, are upgraded at login."""
    if not stored.startswith("$argon2id$"):
        return True
    try:
        return password_hasher.check_needs_rehash(stored)
    except InvalidHashError:
        return True


def password_matches(password: str, stored: str) -> bool:
    if stored.startswith("$argon2"):
        try:
            return password_hasher.verify(stored, password)
        except (VerificationError, InvalidHashError):
            return False
    try:
        scheme, salt_b64, digest_b64 = stored.split("$", 2)
        if scheme != "scrypt":
            return False
        salt, expected = base64.b64decode(salt_b64), base64.b64decode(digest_b64)
        actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def user_record(username: str) -> dict[str, Any] | None:
    with db.connect() as connection:
        row = connection.execute("SELECT username,password_hash,role,enabled,created_at,email,first_name,last_name,avatar_filename,retained_backup_limit,auth_source,auth_provider,external_id,last_login_at FROM portal_users WHERE username=?", (username,)).fetchone()
    return dict(row) if row else None


def account_display_name(username: str) -> str:
    """Return a friendly account label while keeping usernames immutable."""
    account = user_record(username)
    if not account:
        return username
    name = " ".join(part for part in (account.get("first_name"), account.get("last_name")) if part)
    return name or str(account["username"])


def account_has_avatar(username: str) -> bool:
    account = user_record(username)
    return bool(account and safe_avatar_path(str(account.get("avatar_filename") or "")))


def list_users() -> list[dict[str, Any]]:
    with db.connect() as connection:
        rows = connection.execute("SELECT username,role,enabled,created_at,email,first_name,last_name,avatar_filename,retained_backup_limit,auth_source,auth_provider,last_login_at FROM portal_users ORDER BY username COLLATE NOCASE").fetchall()
        role_rows = connection.execute("SELECT username, role_key, source FROM user_roles").fetchall()
    roles: dict[str, list[dict[str, str]]] = {}
    for row in role_rows:
        roles.setdefault(str(row["username"]).lower(), []).append({"role_key": str(row["role_key"]), "source": str(row["source"])})
    users = []
    for row in rows:
        account = dict(row)
        assigned = roles.get(str(account["username"]).lower(), [])
        account["direct_roles"] = sorted({item["role_key"] for item in assigned if item["source"] == "direct"}, key=auth.ROLE_ORDER.index)
        account["group_roles"] = sorted({item["role_key"] for item in assigned if item["source"] != "direct"}, key=auth.ROLE_ORDER.index)
        users.append(account)
    return users


def lab_owner(project_id: str) -> str | None:
    with db.connect() as connection:
        row = connection.execute("SELECT username FROM lab_owners WHERE project_id=?", (project_id,)).fetchone()
    return str(row[0]) if row else None


def set_lab_owner(project_id: str, username: str) -> None:
    with db.connect() as connection:
        connection.execute("INSERT INTO lab_owners(project_id,username,assigned_at) VALUES(?,?,?) ON CONFLICT(project_id) DO UPDATE SET username=excluded.username, assigned_at=excluded.assigned_at", (project_id, username, datetime.now(timezone.utc).isoformat()))


def owned_project_ids(username: str) -> set[str]:
    with db.connect() as connection:
        rows = connection.execute("SELECT project_id FROM lab_owners WHERE username=?", (username,)).fetchall()
    return {str(row[0]) for row in rows}


def shared_project_ids(username: str) -> set[str]:
    with db.connect() as connection:
        rows = connection.execute("SELECT project_id FROM lab_shares WHERE username=?", (username,)).fetchall()
    return {str(row[0]) for row in rows}


def shared_users(project_id: str) -> list[str]:
    with db.connect() as connection:
        rows = connection.execute("SELECT username FROM lab_shares WHERE project_id=? ORDER BY username COLLATE NOCASE", (project_id,)).fetchall()
    return [str(row[0]) for row in rows]


def backup_shared_users(project_id: str, snapshot_id: str) -> list[str]:
    with db.connect() as connection:
        rows = connection.execute("SELECT username FROM backup_shares WHERE project_id=? AND snapshot_id=? ORDER BY username COLLATE NOCASE", (project_id, snapshot_id)).fetchall()
    return [str(row[0]) for row in rows]


def backup_shares_for(username: str) -> list[dict[str, str]]:
    with db.connect() as connection:
        rows = connection.execute("SELECT project_id,snapshot_id,shared_by,shared_at FROM backup_shares WHERE username=? ORDER BY shared_at DESC", (username,)).fetchall()
    return [dict(row) for row in rows]


def retained_backups_for(username: str | None = None) -> list[dict[str, Any]]:
    query, values = "SELECT * FROM retained_backups", ()
    if username:
        query += " WHERE owner=?"; values = (username,)
    query += " ORDER BY retained_at DESC"
    with db.connect() as connection:
        rows = connection.execute(query, values).fetchall()
    return [dict(row) for row in rows]


def retained_backup_limit(username: str) -> int:
    account = user_record(username)
    return max(0, int(account.get("retained_backup_limit") or 2)) if account else 2


def retained_backup_count(username: str) -> int:
    with db.connect() as connection:
        row = connection.execute("SELECT COUNT(*) FROM retained_backups WHERE owner=?", (username,)).fetchone()
    return int(row[0])


def templates_for_user(username: str, include_owned: bool = False) -> list[dict[str, Any]]:
    with db.connect() as connection:
        if include_owned:
            rows = connection.execute("SELECT * FROM lab_templates ORDER BY created_at DESC").fetchall()
        else:
            rows = connection.execute("SELECT DISTINCT t.* FROM lab_templates t LEFT JOIN lab_template_access a ON a.template_id=t.template_id WHERE t.owner=? OR (t.approval_status='approved' AND a.subject_type='local_user' AND a.subject_id=?) ORDER BY t.created_at DESC", (username, username)).fetchall()
    return [dict(row) for row in rows]


def approved_templates() -> list[dict[str, Any]]:
    with db.connect() as connection:
        rows = connection.execute("SELECT * FROM lab_templates WHERE approval_status='approved' ORDER BY created_at DESC").fetchall()
    return [dict(row) for row in rows]


def template_access_users(template_id: str) -> list[str]:
    with db.connect() as connection:
        rows = connection.execute("SELECT subject_id FROM lab_template_access WHERE template_id=? AND subject_type='local_user' ORDER BY subject_id COLLATE NOCASE", (template_id,)).fetchall()
    return [str(row[0]) for row in rows]


def template_record(template_id: str) -> dict[str, Any] | None:
    with db.connect() as connection:
        row = connection.execute("SELECT * FROM lab_templates WHERE template_id=?", (template_id,)).fetchone()
    return dict(row) if row else None




@asynccontextmanager
async def lifespan(app_instance: FastAPI):
    configure_logging()
    init_db()
    # This in-memory value changes on every service start, invalidating every
    # prior browser session even though the session cookie remains on a laptop.
    app_instance.state.session_epoch = secrets.token_urlsafe(24)
    # Reuse a bounded pool for normal controller API calls.  Creating a new
    # AsyncClient for each dashboard request can briefly create hundreds of
    # sockets when several users refresh dashboards with many labs.
    gns3_auth = (GNS3_USERNAME, GNS3_PASSWORD) if GNS3_USERNAME else None
    timeout = httpx.Timeout(GNS3_TIMEOUT_SECONDS, connect=10)
    limits = httpx.Limits(max_connections=40, max_keepalive_connections=12, keepalive_expiry=30)
    app_instance.state.gns3_client = httpx.AsyncClient(timeout=timeout, auth=gns3_auth, limits=limits)
    app_instance.state.backup_jobs = {}
    app_instance.state.import_jobs = {}
    app_instance.state.monitor_disk_samples = {}
    app_instance.state.saml_requests = auth.SamlRequestStore()
    try:
        yield
    finally:
        await app_instance.state.gns3_client.aclose()


app = FastAPI(title="GNS3 Lab Portal", lifespan=lifespan)
# The browser cookie contains only an opaque session token. Setting max_age to
# None makes it a browser-session cookie; server-side expiry/revocation remains
# the authoritative security control.
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, https_only=False, same_site="lax", max_age=None)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


class AuditContextMiddleware:
    """Record the client IP, user agent and a request ID for audit events."""

    def __init__(self, app_instance: Any):
        self.app = app_instance

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in scope.get("headers", [])}
        client_ip = (scope.get("client") or ("", 0))[0] or ""
        forwarded = headers.get("x-forwarded-for", "")
        if forwarded and client_ip in PORTAL_TRUSTED_PROXIES:
            client_ip = forwarded.split(",")[0].strip()
        token = db.request_context.set({
            "source_ip": client_ip[:45], "user_agent": headers.get("user-agent", "")[:512],
            "request_id": uuid.uuid4().hex, "actor": "",
        })
        try:
            await self.app(scope, receive, send)
        finally:
            db.request_context.reset(token)


app.add_middleware(AuditContextMiddleware)
templates = Jinja2Templates(directory=BASE_DIR / "templates")
templates.env.globals["auth_enabled"] = AUTH_ENABLED
templates.env.filters["storage_size"] = format_storage_size


def session_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def revoke_portal_session(request: Request) -> None:
    token = str(request.session.get("sid") or "")
    if token:
        with db.connect() as connection:
            connection.execute("DELETE FROM portal_sessions WHERE session_id_hash=?", (session_token_hash(token),))
    request.session.clear()


def active_portal_session(request: Request) -> dict[str, Any] | None:
    token = str(request.session.get("sid") or "")
    if not token:
        return None
    now = datetime.now(timezone.utc)
    with db.connect() as connection:
        row = connection.execute(
            "SELECT username,issued_at,last_seen_at,expires_at,server_epoch FROM portal_sessions WHERE session_id_hash=?",
            (session_token_hash(token),),
        ).fetchone()
        if not row:
            request.session.clear()
            return None
        record = dict(row)
        try:
            issued_at = datetime.fromisoformat(str(record["issued_at"]).replace("Z", "+00:00"))
            last_seen_at = datetime.fromisoformat(str(record["last_seen_at"]).replace("Z", "+00:00"))
            expires_at = datetime.fromisoformat(str(record["expires_at"]).replace("Z", "+00:00"))
            expired = now >= expires_at or (now - last_seen_at).total_seconds() >= SESSION_IDLE_SECONDS
        except ValueError:
            expired = True
        if expired or record["server_epoch"] != request.app.state.session_epoch:
            connection.execute("DELETE FROM portal_sessions WHERE session_id_hash=?", (session_token_hash(token),))
            request.session.clear()
            return None
        connection.execute("UPDATE portal_sessions SET last_seen_at=? WHERE session_id_hash=?", (now.isoformat(), session_token_hash(token)))
    return record


def create_portal_session(request: Request, username: str) -> None:
    now = datetime.now(timezone.utc)
    token = secrets.token_urlsafe(32)
    expires_at = now.timestamp() + SESSION_MAX_SECONDS
    with db.connect() as connection:
        # One active session per user prevents a previously copied token from
        # remaining valid after that user signs in again.
        connection.execute("DELETE FROM portal_sessions WHERE username=?", (username,))
        connection.execute(
            "INSERT INTO portal_sessions(session_id_hash,username,issued_at,last_seen_at,expires_at,server_epoch) VALUES(?,?,?,?,?,?)",
            (session_token_hash(token), username, now.isoformat(), now.isoformat(), datetime.fromtimestamp(expires_at, timezone.utc).isoformat(), request.app.state.session_epoch),
        )
    request.session.clear()
    request.session["sid"] = token


def current_user(request: Request) -> str:
    """Return the effective portal identity for authorization and ownership."""
    if not AUTH_ENABLED:
        return "anonymous"
    signed_in_user(request)
    user = request.session.get("impersonating") or signed_in_user(request)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    return str(user)


def signed_in_user(request: Request) -> str:
    if not AUTH_ENABLED:
        return "anonymous"
    session = active_portal_session(request)
    if not session:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    account = user_record(str(session["username"]))
    if not account or not account.get("enabled"):
        revoke_portal_session(request)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    impersonating = request.session.get("impersonating")
    db.current_context()["actor"] = f"{account['username']} impersonating {impersonating}" if impersonating else str(account["username"])
    return str(account["username"])


def has_permission(request: Request, permission: str) -> bool:
    """Permission of the effective user (the impersonated user while viewing as someone)."""
    if not AUTH_ENABLED:
        return True
    return permission in auth.user_permissions(current_user(request))


def admin_permission(request: Request, permission: str) -> bool:
    """Permission of the signed-in user for portal-wide actions; never while impersonating."""
    if not AUTH_ENABLED:
        return True
    if request.session.get("impersonating"):
        return False
    return permission in auth.user_permissions(signed_in_user(request))


def require_permission(request: Request, permission: str, portal_wide: bool = False) -> None:
    allowed = admin_permission(request, permission) if portal_wide else has_permission(request, permission)
    if not allowed:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Your role does not allow this ({auth.PERMISSIONS.get(permission, permission)}).")


def is_admin(request: Request) -> bool:
    """True for a signed-in user who may manage users, even while viewing as another user."""
    if not AUTH_ENABLED:
        return True
    return "users.manage" in auth.user_permissions(signed_in_user(request))


def can_manage_users(request: Request) -> bool:
    """UI permission: user administration, outside impersonation mode."""
    return bool(AUTH_ENABLED and admin_permission(request, "users.manage"))


def has_admin_scope(request: Request) -> bool:
    """See and manage every lab, unless deliberately operating as another user."""
    return admin_permission(request, "labs.all")


def require_lab_owner(request: Request, project_id: str) -> None:
    """Destructive lab actions: the owner, or someone who manages every lab."""
    if AUTH_ENABLED and not has_admin_scope(request) and lab_owner(project_id) != current_user(request):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the lab owner or a Lab Manager can do this.")


def can_manage_lab_sharing(request: Request, project_id: str) -> bool:
    return bool(has_admin_scope(request) or lab_owner(project_id) == current_user(request))


def audit_actor(request: Request) -> str:
    signed_in = signed_in_user(request)
    effective = current_user(request)
    return signed_in if signed_in == effective else f"{signed_in} impersonating {effective}"


def require_admin(request: Request) -> None:
    if not is_admin(request):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Platform Admin access is required.")


def require_project_access(request: Request, project_id: str) -> None:
    if not AUTH_ENABLED or has_admin_scope(request):
        return
    if project_id not in owned_project_ids(current_user(request)) | shared_project_ids(current_user(request)):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lab not found.")


def redirect_login() -> RedirectResponse:
    return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)


async def gns3(method: str, path: str, **kwargs: Any) -> Any:
    started = perf_counter()
    try:
        response = await app.state.gns3_client.request(method, f"{GNS3_URL}{path}", **kwargs)
    except httpx.RequestError as exc:
        api_logger.error("request method=%s path=%s outcome=request_error duration_ms=%d error=%s", method, path, int((perf_counter() - started) * 1000), safe_api_detail(exc))
        raise HTTPException(503, f"Cannot reach the GNS3 server: {exc}") from exc
    duration_ms = int((perf_counter() - started) * 1000)
    if response.status_code >= 400:
        try:
            detail = response.json()
        except ValueError:
            detail = response.text
        api_logger.warning("request method=%s path=%s status=%s duration_ms=%d detail=%s", method, path, response.status_code, duration_ms, safe_api_detail(detail))
        raise HTTPException(response.status_code, f"GNS3 rejected the request: {detail}")
    api_logger.info("request method=%s path=%s status=%s duration_ms=%d", method, path, response.status_code, duration_ms)
    return response.json() if response.content else None


async def ensure_project_open(project_id: str) -> bool:
    """Open a project when GNS3 requires an opened project for an action.

    Snapshot operations are rejected by GNS3 for closed projects. Opening a
    project does not start its nodes, so it is safe before a backup, restore,
    or deletion. The project is left open afterward so the portal never closes
    a lab that an administrator may be managing in GNS3.
    """
    project = await gns3("GET", f"/v2/projects/{project_id}")
    if str(project.get("status", "")).lower() == "opened":
        return False
    await gns3("POST", f"/v2/projects/{project_id}/open")
    return True


ALWAYS_ACTIVE_NODE_TYPES = frozenset({"cloud", "ethernet_switch"})


def is_non_stoppable_node(node: dict[str, Any]) -> bool:
    """Return whether a built-in GNS3 node intentionally remains started.

    Cloud nodes are host-network attachments. GNS3's built-in Ethernet switch
    also always serializes with status ``started`` and has no meaningful
    stopped state. Neither is an emulated workload, so waiting for either one
    would make a safe backup time out forever. Routers, VMs, containers, and
    every other node type still must report ``stopped`` before a snapshot.
    """
    return str(node.get("node_type", "")).strip().lower() in ALWAYS_ACTIVE_NODE_TYPES


async def stop_all_nodes(project_id: str, job: dict[str, Any] | None = None) -> tuple[int, int, int]:
    """Stop emulated nodes while allowing built-in always-active nodes."""
    nodes = await gns3("GET", f"/v2/projects/{project_id}/nodes")
    ignored_nodes = [node for node in nodes if is_non_stoppable_node(node)]
    running = [
        node for node in nodes
        if not is_non_stoppable_node(node) and node.get("status") != "stopped"
    ]
    if job is not None:
        ignored_note = f" ({len(ignored_nodes)} built-in node(s) left active)" if ignored_nodes else ""
        job.update(
            progress_percent=10,
            message=f"Stopping {len(running)} running node(s)…{ignored_note}",
        )
    portal_logger.info(
        "backup_stop_requested project_id=%s running_nodes=%s ignored_built_in_nodes=%s",
        project_id,
        len(running),
        safe_api_detail([
            {"id": node.get("node_id"), "name": node.get("name"), "node_type": node.get("node_type"), "status": node.get("status")}
            for node in ignored_nodes
        ]),
    )
    for node in running:
        if job and job.get("cancel_requested"):
            raise HTTPException(409, "Backup cancelled before the snapshot was started.")
        portal_logger.info("backup_stop_node project_id=%s node_id=%s name=%s status=%s", project_id, node.get("node_id"), safe_api_detail(node.get("name", ""), 120), node.get("status"))
        await gns3("POST", f"/v2/projects/{project_id}/nodes/{node['node_id']}/stop")

    # A stop request is asynchronous for some appliance types. Never create a
    # backup while an emulated node still reports as running. Built-in nodes
    # that are permanently active are intentionally excluded (see above).
    stoppable_count = len(nodes) - len(ignored_nodes)
    for _ in range(60):
        if job and job.get("cancel_requested"):
            raise HTTPException(409, "Backup cancelled before the snapshot was started.")
        current_nodes = await gns3("GET", f"/v2/projects/{project_id}/nodes")
        still_running = [
            node for node in current_nodes
            if not is_non_stoppable_node(node) and node.get("status") != "stopped"
        ]
        if not still_running:
            portal_logger.info("backup_nodes_stopped project_id=%s stopped_by_portal=%s", project_id, len(running))
            return len(nodes), len(running), len(ignored_nodes)
        if job is not None:
            stopped = stoppable_count - len(still_running)
            ignored_note = f"; {len(ignored_nodes)} built-in node(s) remain active" if ignored_nodes else ""
            job.update(
                progress_percent=10 + int(25 * stopped / max(stoppable_count, 1)),
                message=f"Waiting for {len(still_running)} node(s) to stop…{ignored_note}",
            )
        portal_logger.info(
            "backup_waiting project_id=%s poll=%s still_running=%s nodes=%s",
            project_id, _ + 1, len(still_running),
            safe_api_detail([{"id": node.get("node_id"), "name": node.get("name"), "status": node.get("status")} for node in still_running]),
        )
        await asyncio.sleep(1)
    portal_logger.error("backup_stop_timeout project_id=%s timeout_seconds=60", project_id)
    raise HTTPException(409, "Backup was not created because one or more stoppable nodes did not stop within 60 seconds.")


ACTIVE_BACKUP_STATES = frozenset({"queued", "stopping_nodes", "creating_snapshot", "cancelling"})


def backup_is_active(project_id: str) -> bool:
    """Return whether the portal is currently changing this lab for a backup."""
    return any(
        job.get("project_id") == project_id and job.get("status") in ACTIVE_BACKUP_STATES
        for job in app.state.backup_jobs.values()
    )


async def set_lab_node_power(project_id: str, action: str) -> tuple[int, int]:
    """Start or stop emulated nodes, leaving built-in active nodes untouched."""
    if action not in {"start", "stop"}:
        raise ValueError("Unsupported lab power action")
    await ensure_project_open(project_id)
    nodes = await gns3("GET", f"/v2/projects/{project_id}/nodes")
    ignored = [node for node in nodes if is_non_stoppable_node(node)]
    targets = [
        node for node in nodes
        if not is_non_stoppable_node(node)
        and (node.get("status") == "stopped" if action == "start" else node.get("status") != "stopped")
    ]
    portal_logger.info(
        "lab_%s_requested project_id=%s target_nodes=%s ignored_built_in_nodes=%s",
        action, project_id, safe_api_detail([
            {"id": node.get("node_id"), "name": node.get("name"), "status": node.get("status")}
            for node in targets
        ]), len(ignored),
    )
    for node in targets:
        await gns3("POST", f"/v2/projects/{project_id}/nodes/{node['node_id']}/{action}")
    portal_logger.info("lab_%s_dispatched project_id=%s node_count=%s", action, project_id, len(targets))
    return len(targets), len(ignored)


def directory_size(path: Path) -> int:
    """Return size of regular files beneath path without following symlinks."""
    try:
        if path.is_file() and not path.is_symlink():
            return path.stat().st_size
    except OSError:
        return 0
    total = 0
    try:
        for entry in path.rglob("*"):
            try:
                if entry.is_file() and not entry.is_symlink():
                    total += entry.stat().st_size
            except OSError:
                continue
    except OSError:
        return 0
    return total


def snapshot_storage(project: dict[str, Any], snapshots: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int, bool]:
    """Add safe local filesystem size data to controller snapshot records."""
    if not GNS3_PROJECTS_DIR:
        return snapshots, 0, False
    try:
        root = GNS3_PROJECTS_DIR.resolve(strict=True)
        project_path = Path(str(project.get("path", ""))).resolve(strict=True)
        project_path.relative_to(root)
    except (OSError, ValueError):
        return snapshots, 0, False

    snapshots_dir = project_path / "snapshots"
    if not snapshots_dir.is_dir():
        return snapshots, 0, True
    try:
        resolved_snapshots_dir = snapshots_dir.resolve(strict=True)
        # A symlink must resolve into the configured HDD root. A normal
        # directory inside this project is also safe to inspect, which keeps
        # legacy, not-yet-migrated backup sizes and dates visible.
        if snapshots_dir.is_symlink():
            if not GNS3_SNAPSHOTS_DIR:
                return snapshots, 0, False
            resolved_snapshots_dir.relative_to(GNS3_SNAPSHOTS_DIR.resolve(strict=True))
        else:
            resolved_snapshots_dir.relative_to(project_path)
    except (OSError, ValueError):
        return snapshots, 0, False

    try:
        # Iterate the resolved directory so files behind the project snapshots
        # symlink are scanned consistently on a dedicated HDD.
        entries = {entry.name: entry for entry in resolved_snapshots_dir.iterdir()}
    except OSError:
        return snapshots, 0, True
    total = directory_size(resolved_snapshots_dir)
    enriched: list[dict[str, Any]] = []
    for snapshot in snapshots:
        item = dict(snapshot)
        # GNS3 exposes a UUID as ``snapshot_id`` through its API, but stores
        # native snapshots on disk using their display name, for example:
        # ``Before firewall changes.gns3snapshot``.  Check the ID first for
        # compatibility with other storage layouts, then the native filename.
        # Looking up pre-enumerated directory entries (rather than joining an
        # untrusted name to a path) also prevents path traversal.
        snapshot_id = str(item.get("snapshot_id", ""))
        snapshot_name = str(item.get("name", ""))
        candidates = (snapshot_id, snapshot_name, f"{snapshot_name}.gns3snapshot")
        stored = next((entries[candidate] for candidate in candidates if candidate and candidate in entries), None)
        item["size_bytes"] = directory_size(stored) if stored else None
        # GNS3 does not consistently return a creation timestamp.  The native
        # snapshot file's mtime is the most reliable completion time locally.
        if stored:
            try:
                item["completed_at"] = datetime.fromtimestamp(stored.stat().st_mtime, timezone.utc).isoformat()
            except OSError:
                item["completed_at"] = item.get("created_at")
        else:
            item["completed_at"] = item.get("created_at")
        enriched.append(item)
    enriched.sort(key=lambda item: str(item.get("completed_at") or ""))
    return enriched, total, True


def format_backup_datetime(value: Any) -> str:
    """Return an ISO timestamp; the browser renders it in its local timezone."""
    if not value:
        return "—"
    try:
        # GNS3 v2 reports snapshot created_at as Unix epoch seconds.
        if isinstance(value, (int, float)) or str(value).isdigit():
            return datetime.fromtimestamp(float(value), timezone.utc).isoformat()
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        return str(value)


templates.env.filters["backup_datetime"] = format_backup_datetime
templates.env.filters["account_display_name"] = account_display_name
templates.env.filters["account_has_avatar"] = account_has_avatar
templates.env.filters["role_name"] = auth.role_name
templates.env.globals["can"] = has_permission
templates.env.globals["admin_can"] = admin_permission
templates.env.globals["ROLES"] = auth.ROLES
templates.env.globals["PERMISSIONS"] = auth.PERMISSIONS


def local_snapshot_file(project: dict[str, Any], snapshot: dict[str, Any]) -> Path | None:
    """Return a verified native snapshot path, or None when local access is unavailable.

    GNS3 native snapshot files are ZIP-compatible archives named after the
    visible snapshot name.  This portal deliberately only reads a direct entry
    from the project's snapshots directory; it never accepts a browser-supplied
    filesystem path.
    """
    if not GNS3_PROJECTS_DIR:
        return None
    try:
        root = GNS3_PROJECTS_DIR.resolve(strict=True)
        project_path = Path(str(project.get("path", ""))).resolve(strict=True)
        project_path.relative_to(root)
        configured_snapshots_dir = project_path / "snapshots"
        snapshots_dir = configured_snapshots_dir.resolve(strict=True)
        if configured_snapshots_dir.is_symlink():
            if not GNS3_SNAPSHOTS_DIR:
                return None
            snapshots_dir.relative_to(GNS3_SNAPSHOTS_DIR.resolve(strict=True))
        else:
            snapshots_dir.relative_to(project_path)
    except (OSError, ValueError):
        return None

    snapshot_id = str(snapshot.get("snapshot_id", ""))
    snapshot_name = str(snapshot.get("name", ""))
    candidates = (snapshot_id, snapshot_name, f"{snapshot_name}.gns3snapshot")
    for candidate in candidates:
        if not candidate:
            continue
        try:
            stored = (snapshots_dir / candidate).resolve(strict=True)
            stored.relative_to(snapshots_dir)
        except (OSError, ValueError):
            continue
        if stored.is_file() and not stored.is_symlink():
            return stored
    return None


def ensure_hdd_snapshot_link(project: dict[str, Any]) -> tuple[bool, str]:
    """Link an empty new portal lab to its dedicated HDD snapshot directory.

    Existing non-empty directories are never moved or replaced. They remain
    safe for a separate migration during a GNS3 maintenance window.
    """
    if not GNS3_PROJECTS_DIR or not GNS3_SNAPSHOTS_DIR:
        return False, "HDD snapshot automation is not configured"
    project_id = str(project.get("project_id", "")).strip()
    if not project_id or Path(project_id).name != project_id:
        return False, "controller returned an invalid project identifier"
    try:
        projects_root = GNS3_PROJECTS_DIR.resolve(strict=True)
        snapshots_root = GNS3_SNAPSHOTS_DIR.resolve(strict=True)
        project_path = Path(str(project.get("path", ""))).resolve(strict=True)
        project_path.relative_to(projects_root)
        snapshots_path = project_path / "snapshots"
        if snapshots_path.is_symlink():
            snapshots_path.resolve(strict=True).relative_to(snapshots_root)
            return True, "snapshot directory is already linked to the HDD"
        if snapshots_path.exists() and not snapshots_path.is_dir():
            return False, "project snapshots path is not a directory"
        if snapshots_path.exists() and any(snapshots_path.iterdir()):
            return False, "project snapshots directory is not empty"

        target = snapshots_root / project_id
        target.mkdir(parents=True, exist_ok=True)
        temporary_link = project_path / f".snapshots-portal-{uuid.uuid4().hex}"
        temporary_link.symlink_to(target, target_is_directory=True)
        try:
            if snapshots_path.exists():
                snapshots_path.rmdir()
            temporary_link.replace(snapshots_path)
        finally:
            if temporary_link.is_symlink():
                temporary_link.unlink(missing_ok=True)
        return True, "snapshot directory linked to the HDD"
    except (OSError, ValueError) as exc:
        return False, f"could not create HDD snapshot link: {exc}"


async def stream_project_export(project_id: str):
    """Relay a controller project export without buffering it in portal memory."""
    gns3_auth = (GNS3_USERNAME, GNS3_PASSWORD) if GNS3_USERNAME else None
    timeout = httpx.Timeout(GNS3_TIMEOUT_SECONDS, connect=10)
    async with httpx.AsyncClient(timeout=timeout, auth=gns3_auth) as client:
        async with client.stream("GET", f"{GNS3_URL}/v2/projects/{project_id}/export") as response:
            if response.status_code >= 400:
                detail = (await response.aread()).decode(errors="replace")
                raise HTTPException(response.status_code, f"GNS3 rejected the export request: {detail}")
            async for chunk in response.aiter_bytes():
                yield chunk


async def import_snapshot_as_new_project(job: dict[str, Any], snapshot_path: Path) -> dict[str, Any]:
    """Upload a native snapshot to GNS3 as a distinct, powered-off project."""
    total_bytes = snapshot_path.stat().st_size
    sent_bytes = 0

    async def file_body():
        nonlocal sent_bytes
        with snapshot_path.open("rb") as source:
            while chunk := await asyncio.to_thread(source.read, 1024 * 1024):
                sent_bytes += len(chunk)
                job.update(
                    progress_percent=min(94, 30 + int(64 * sent_bytes / max(total_bytes, 1))),
                    message=f"Copying backup into GNS3 ({format_storage_size(sent_bytes)} sent)…",
                )
                yield chunk

    gns3_auth = (GNS3_USERNAME, GNS3_PASSWORD) if GNS3_USERNAME else None
    timeout = httpx.Timeout(GNS3_TIMEOUT_SECONDS, connect=10)
    params = {
        "name": str(job["new_name"]),
        "auto_start": "false",
        "auto_open": "false",
        "auto_close": "true",
    }
    try:
        async with httpx.AsyncClient(timeout=timeout, auth=gns3_auth) as client:
            response = await client.post(
                f"{GNS3_URL}/v2/projects/{job['new_project_id']}/import",
                params=params,
                content=file_body(),
                headers={"Content-Type": "application/gns3project"},
            )
    except httpx.RequestError as exc:
        raise HTTPException(503, f"Cannot reach the GNS3 server: {exc}") from exc
    if response.status_code >= 400:
        try:
            detail = response.json()
        except ValueError:
            detail = response.text
        raise HTTPException(response.status_code, f"GNS3 rejected the import request: {detail}")
    return response.json()


async def run_snapshot_import_job(job: dict[str, Any]) -> None:
    """Clone a selected snapshot into a new project without touching its source lab."""
    source_project_id = str(job.get("source_project_id", ""))
    snapshot_id = str(job.get("snapshot_id", ""))
    try:
        if job.get("upload_path"):
            snapshot_path = Path(str(job["upload_path"])).resolve(strict=True)
            snapshot_path.relative_to(UPLOAD_DIR.resolve(strict=True))
            job.update(status="preparing", progress_percent=5, message="Preparing the uploaded backup…")
        elif job.get("retained_path"):
            snapshot_path = Path(str(job["retained_path"])).resolve(strict=True)
            snapshot_path.relative_to(RETAINED_BACKUP_DIR.resolve(strict=True))
            job.update(status="preparing", progress_percent=5, message="Preparing the retained backup…")
        elif job.get("template_path"):
            snapshot_path = Path(str(job["template_path"])).resolve(strict=True)
            snapshot_path.relative_to(LAB_TEMPLATE_DIR.resolve(strict=True))
            job.update(status="preparing", progress_percent=5, message="Preparing the lab template…")
        else:
            job.update(status="preparing", progress_percent=5, message="Looking up the selected backup…")
            project = await gns3("GET", f"/v2/projects/{source_project_id}")
            snapshots = await gns3("GET", f"/v2/projects/{source_project_id}/snapshots")
            snapshot = next((item for item in snapshots if str(item.get("snapshot_id")) == snapshot_id), None)
            if not snapshot:
                raise HTTPException(404, "The selected backup no longer exists.")
            snapshot_path = local_snapshot_file(project, snapshot)
            if not snapshot_path:
                raise HTTPException(409, "This backup cannot be read locally. Configure GNS3_PROJECTS_DIR and grant the portal read access to the snapshots directory.")
        job.update(status="importing", progress_percent=25, message="Creating the new lab from the selected backup…")
        imported = await import_snapshot_as_new_project(job, snapshot_path)
        imported_id = str(imported.get("project_id", job["new_project_id"]))
        set_lab_owner(imported_id, str(job["actor"]))
        linked, link_reason = await asyncio.to_thread(
            ensure_hdd_snapshot_link,
            await gns3("GET", f"/v2/projects/{imported_id}"),
        )
        if linked:
            audit(str(job.get("audit_actor", job["actor"])), "link_lab_snapshots_to_hdd", imported_id, {"reason": link_reason})
        else:
            portal_logger.warning("snapshot_hdd_link_skipped project_id=%s reason=%s", imported_id, link_reason)
        # A shared backup is a single-use lab template, not ongoing access to
        # the source lab.  Consume only the recipient's matching grant after
        # the new project is created successfully; a failed import remains
        # retryable and does not remove the share.
        if source_project_id and snapshot_id and job.get("consume_backup_share"):
            with db.connect() as connection:
                connection.execute(
                    "DELETE FROM backup_shares WHERE project_id=? AND snapshot_id=? AND username=?",
                    (source_project_id, snapshot_id, str(job["actor"])),
                )
            audit(str(job.get("audit_actor", job["actor"])), "consume_backup_template", source_project_id, {
                "snapshot_id": snapshot_id, "created_project_id": imported_id,
            })
        job.update(status="completed", progress_percent=100, message="New lab created. It is powered off.", project_id=imported_id)
        audit(str(job.get("audit_actor", job["actor"])), "create_lab_from_backup", imported_id, {
            "source_project_id": source_project_id,
            "snapshot_id": snapshot_id,
            "uploaded_backup": bool(job.get("upload_path")),
            "new_name": job["new_name"],
        })
    except HTTPException as exc:
        job.update(status="failed", message=str(exc.detail))
    except Exception:
        job.update(status="failed", message="The portal encountered an unexpected error while importing the backup. Check the service log for details.")
        portal_logger.exception("backup_import_unexpected_error source_project_id=%s", source_project_id)
    finally:
        if job.get("upload_path"):
            try:
                Path(str(job["upload_path"])).unlink(missing_ok=True)
            except OSError:
                portal_logger.warning("uploaded_backup_cleanup_failed path=%s", safe_api_detail(job["upload_path"]))


async def run_backup_job(job: dict[str, Any]) -> None:
    """Perform a safe backup independently of the browser/proxy connection."""
    project_id = str(job["project_id"])
    try:
        portal_logger.info("backup_started project_id=%s snapshot_name=%s actor=%s", project_id, safe_api_detail(job["name"], 120), job["actor"])
        job.update(status="stopping_nodes", progress_percent=5, message="Opening the lab in GNS3…")
        opened_by_portal = await ensure_project_open(project_id)
        opening_note = " The portal opened the previously closed lab." if opened_by_portal else ""
        job.update(status="stopping_nodes", progress_percent=5, message=f"Stopping all lab nodes…{opening_note}")
        node_count, stopped_count, ignored_count = await stop_all_nodes(project_id, job)
        if job.get("cancel_requested"):
            raise HTTPException(409, "Backup cancelled before the snapshot was started.")
        job.update(
            status="creating_snapshot", progress_percent=35, progress_is_estimated=True,
            message="GNS3 is creating the native snapshot. Progress is an estimate because GNS3 does not publish byte-level progress.",
        )
        snapshot_started = asyncio.get_running_loop().time()

        async def update_snapshot_progress() -> None:
            """Update the visible estimate every five seconds during the controller call."""
            while True:
                await asyncio.sleep(5)
                elapsed = asyncio.get_running_loop().time() - snapshot_started
                estimated = min(95, 35 + int(60 * elapsed / SNAPSHOT_ESTIMATED_SECONDS))
                minutes, seconds = divmod(int(elapsed), 60)
                job.update(
                    progress_percent=estimated, progress_is_estimated=True,
                    message=f"GNS3 is creating the native snapshot ({minutes}m {seconds:02d}s elapsed; estimated progress).",
                )

        progress_task = asyncio.create_task(update_snapshot_progress())
        try:
            snapshot = await gns3("POST", f"/v2/projects/{project_id}/snapshots", json={"name": job["name"]})
        finally:
            progress_task.cancel()
            try:
                await progress_task
            except asyncio.CancelledError:
                pass
        if job.get("cancel_requested"):
            # GNS3's snapshot endpoint has no cancellation API.  Once it has
            # begun, let it finish safely and remove the resulting snapshot.
            job.update(status="cancelling", message="GNS3 finished safely; removing the cancelled backup…")
            await ensure_project_open(project_id)
            await gns3("DELETE", f"/v2/projects/{project_id}/snapshots/{snapshot.get('snapshot_id')}")
            job.update(status="cancelled", progress_percent=0, progress_is_estimated=False, message="Backup cancelled. The completed snapshot was removed.", snapshot_id=None, completed_at=datetime.now(timezone.utc).isoformat())
            portal_logger.info("backup_cancelled_after_snapshot project_id=%s snapshot_id=%s", project_id, snapshot.get("snapshot_id"))
            return
        job.update(status="completed", progress_percent=100, progress_is_estimated=False, message="Backup completed.", snapshot_id=snapshot.get("snapshot_id"), completed_at=datetime.now(timezone.utc).isoformat())
        portal_logger.info("backup_completed project_id=%s snapshot_id=%s", project_id, snapshot.get("snapshot_id"))
        audit(str(job.get("audit_actor", job["actor"])), "create_safe_backup", project_id, {"snapshot": snapshot, "node_count": node_count, "nodes_stopped": stopped_count, "non_stoppable_nodes": ignored_count})
    except HTTPException as exc:
        cancelled = bool(job.get("cancel_requested"))
        job.update(status="cancelled" if cancelled else "failed", message=str(exc.detail), completed_at=datetime.now(timezone.utc).isoformat())
        portal_logger.warning("backup_failed project_id=%s detail=%s", project_id, safe_api_detail(exc.detail))
    except Exception:
        job.update(status="failed", message="The portal encountered an unexpected error while creating the backup. Check the service log for details.")
        portal_logger.exception("backup_unexpected_error project_id=%s", project_id)


def error_page(request: Request, exc: HTTPException) -> HTMLResponse:
    context: dict[str, Any] = {"message": str(exc.detail), "status": exc.status_code}
    try:
        context.update(base_context(request))
    except HTTPException:
        pass  # Not signed in: show the error without account details.
    return templates.TemplateResponse(request, "error.html", context, status_code=exc.status_code)


def login_context(error: str | None = None) -> dict[str, Any]:
    return {
        "error": error, "login_page": True,
        "saml_providers": auth.list_providers("saml", enabled_only=True) if PORTAL_PUBLIC_URL else [],
        "ldap_enabled": bool(auth.list_providers("ldap", enabled_only=True)),
    }


def login_failed(request: Request, login_name: str, reason: str, message: str = "Invalid username or password.", **details: Any) -> HTMLResponse:
    audit(login_name[:255] or "(blank)", "login_failed", "portal", {"reason": reason, **details}, outcome="failure")
    return templates.TemplateResponse(request, "login.html", login_context(message), status_code=401)


def provision_external_user(provider: dict[str, Any], identity: auth.ExternalIdentity) -> tuple[dict[str, Any] | None, str]:
    """Create or refresh a directory/SSO user and its group roles. Returns (account, failure reason)."""
    account = user_record(identity.username)
    if account and (account.get("auth_source") != provider["kind"] or account.get("auth_provider") != provider["provider_id"]):
        # Never let an external identity take over a local account or another provider's account.
        return None, "username_conflict"
    group_roles = auth.roles_for_groups(provider, identity.groups)
    direct_roles = {key for key, sources in auth.user_roles(identity.username).items() if "direct" in sources} if account else set()
    if not group_roles and not direct_roles:
        if account:
            auth.set_group_roles(account["username"], provider["provider_id"], set())  # drop stale group roles
        return None, "no_role"
    now = datetime.now(timezone.utc).isoformat()
    with db.connect() as connection:
        if not account:
            connection.execute(
                "INSERT INTO portal_users(username,password_hash,role,enabled,created_at,email,first_name,last_name,auth_source,auth_provider,external_id,last_login_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (identity.username, auth.EXTERNAL_PASSWORD_MARKER, "user", 1, now, identity.email or None, identity.first_name or None, identity.last_name or None, provider["kind"], provider["provider_id"], identity.external_id, now),
            )
        else:
            if not account.get("enabled"):
                return None, "disabled"
            connection.execute(
                "UPDATE portal_users SET email=COALESCE(?,email), first_name=COALESCE(?,first_name), last_name=COALESCE(?,last_name), external_id=?, last_login_at=? WHERE username=?",
                (identity.email or None, identity.first_name or None, identity.last_name or None, identity.external_id, now, account["username"]),
            )
    username = account["username"] if account else identity.username
    auth.set_group_roles(username, provider["provider_id"], group_roles)
    if not account:
        audit(username, "create_external_user", username, {"provider": provider["provider_id"], "auth_source": provider["kind"], "roles": sorted(group_roles)})
    return user_record(username), ""


def complete_login(request: Request, account: dict[str, Any], method: str, provider_id: str = "") -> RedirectResponse:
    db.current_context()["actor"] = str(account["username"])
    with db.connect() as connection:
        connection.execute("UPDATE portal_users SET last_login_at=? WHERE username=?", (datetime.now(timezone.utc).isoformat(), account["username"]))
    create_portal_session(request, str(account["username"]))
    roles = sorted(auth.user_roles(str(account["username"])))
    audit(str(account["username"]), "login", "portal", {"method": method, "provider": provider_id or None, "roles": roles})
    return RedirectResponse("/", status_code=303)


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(request: Request, exc: StarletteHTTPException):
    """Browsers get the portal's error page (or the sign-in page); scripts get JSON."""
    if "text/html" not in request.headers.get("accept", ""):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=getattr(exc, "headers", None))
    if exc.status_code == status.HTTP_401_UNAUTHORIZED and AUTH_ENABLED:
        return redirect_login()
    return error_page(request, HTTPException(exc.status_code, str(exc.detail)))


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if not AUTH_ENABLED:
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(request, "login.html", login_context(request.query_params.get("error")))


@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    if not AUTH_ENABLED:
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    login_name = username.strip()
    account = user_record(login_name) or user_record(auth.normalise_login_name(login_name))
    if account and account.get("auth_source", "local") == "local":
        if not account.get("enabled"):
            return login_failed(request, login_name, "disabled")
        if not password_matches(password, str(account.get("password_hash", ""))):
            return login_failed(request, login_name, "bad_password")
        if password_needs_rehash(str(account["password_hash"])):
            db.current_context()["actor"] = str(account["username"])
            with db.connect() as connection:
                connection.execute("UPDATE portal_users SET password_hash=? WHERE username=?", (password_hash(password), account["username"]))
            audit(str(account["username"]), "password_rehashed", str(account["username"]), {"scheme": "argon2id"})
        return complete_login(request, account, "local")
    if account and account.get("auth_source") == "saml":
        return login_failed(request, login_name, "sso_account", "This account signs in with single sign-on. Use the sign-in button for your organisation.")
    providers = auth.list_providers("ldap", enabled_only=True)
    if account and account.get("auth_source") == "ldap":
        providers = [item for item in providers if item["provider_id"] == account.get("auth_provider")]
    for provider in providers:
        directory = auth.LdapDirectory(provider, secret_box)
        try:
            identity = await asyncio.to_thread(directory.authenticate, login_name, password)
        except auth.IdentityError as exc:
            portal_logger.error("ldap_login_error provider=%s error=%s", provider["provider_id"], exc)
            audit(login_name[:255], "login_failed", "portal", {"reason": "directory_unavailable", "provider": provider["provider_id"]}, outcome="error")
            continue
        if identity is None:
            continue
        account, reason = provision_external_user(provider, identity)
        if not account:
            message = "Your directory account has no portal role. Ask an administrator to map one of your groups." if reason == "no_role" else "Invalid username or password."
            return login_failed(request, login_name, reason, message, provider=provider["provider_id"])
        return complete_login(request, account, "ldap", provider["provider_id"])
    return login_failed(request, login_name, "unknown_user" if not account else "bad_password")


def saml_auth_for(request: Request, provider: dict[str, Any], post_data: dict[str, str] | None = None) -> Any:
    from onelogin.saml2.auth import OneLogin_Saml2_Auth
    request_data = auth.saml_request_data(PORTAL_PUBLIC_URL, request.url.path, dict(request.query_params), post_data or {})
    return OneLogin_Saml2_Auth(request_data, auth.saml_settings(provider, PORTAL_PUBLIC_URL))


def enabled_saml_provider(provider_id: str) -> dict[str, Any]:
    provider = auth.get_provider(provider_id)
    if not provider or provider["kind"] != "saml" or not provider["enabled"] or not PORTAL_PUBLIC_URL:
        raise HTTPException(404, "Single sign-on provider not found.")
    return provider


@app.get("/auth/saml/{provider_id}/login")
async def saml_login(request: Request, provider_id: str):
    if not AUTH_ENABLED:
        return RedirectResponse("/", status_code=303)
    provider = enabled_saml_provider(provider_id)
    saml = saml_auth_for(request, provider)
    relay = auth.SamlRequestStore.new_relay_token()
    url = saml.login(return_to=relay)
    app.state.saml_requests.put(relay, provider_id, saml.get_last_request_id())
    return RedirectResponse(url, status_code=303)


@app.post("/auth/saml/{provider_id}/acs")
async def saml_acs(request: Request, provider_id: str):
    if not AUTH_ENABLED:
        return RedirectResponse("/", status_code=303)
    provider = enabled_saml_provider(provider_id)
    form = {key: str(value) for key, value in (await request.form()).items()}
    pending = app.state.saml_requests.take(form.get("RelayState", ""))
    if not pending or pending[0] != provider_id:
        # Only sign-ins started from this portal are accepted (no IdP-initiated SSO),
        # which lets every response be bound to its request ID.
        return login_failed(request, "(saml)", "saml_unsolicited", "That sign-in has expired. Start again from the portal sign-in page.", provider=provider_id)
    saml = saml_auth_for(request, provider, form)
    try:
        saml.process_response(request_id=pending[1])
    except Exception as exc:  # malformed XML and similar
        portal_logger.warning("saml_response_rejected provider=%s error=%s", provider_id, type(exc).__name__)
        return login_failed(request, "(saml)", "saml_invalid", "Single sign-on failed. Try again or contact an administrator.", provider=provider_id)
    if saml.get_errors() or not saml.is_authenticated():
        portal_logger.warning("saml_response_rejected provider=%s errors=%s reason=%s", provider_id, saml.get_errors(), saml.get_last_error_reason())
        return login_failed(request, "(saml)", "saml_invalid", "Single sign-on failed. Try again or contact an administrator.", provider=provider_id, errors=saml.get_errors())
    if not app.state.saml_requests.mark_assertion_used(saml.get_last_assertion_id() or ""):
        return login_failed(request, "(saml)", "saml_replay", "That sign-in response was already used.", provider=provider_id)
    try:
        identity = auth.saml_identity(provider, saml.get_nameid() or "", saml.get_attributes())
    except auth.IdentityError as exc:
        return login_failed(request, "(saml)", "saml_no_username", str(exc), provider=provider_id)
    account, reason = provision_external_user(provider, identity)
    if not account:
        message = "Your account has no portal role. Ask an administrator to map one of your groups." if reason == "no_role" else "Single sign-on failed for this account."
        return login_failed(request, identity.username, reason, message, provider=provider_id)
    return complete_login(request, account, "saml", provider_id)


@app.get("/auth/saml/{provider_id}/metadata")
async def saml_metadata(request: Request, provider_id: str):
    provider = auth.get_provider(provider_id)
    if not provider or provider["kind"] != "saml" or not PORTAL_PUBLIC_URL:
        raise HTTPException(404, "Single sign-on provider not found.")
    from onelogin.saml2.settings import OneLogin_Saml2_Settings
    settings = OneLogin_Saml2_Settings(auth.saml_settings(provider, PORTAL_PUBLIC_URL), sp_validation_only=True)
    metadata = settings.get_sp_metadata()
    return HTMLResponse(metadata, media_type="application/samlmetadata+xml")


@app.post("/logout")
async def logout(request: Request):
    if not AUTH_ENABLED:
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    session = active_portal_session(request)
    user = str(session["username"]) if session else "unknown"
    revoke_portal_session(request)
    audit(str(user), "logout", "portal")
    return redirect_login()


@app.get("/profile", response_class=HTMLResponse)
async def profile_page(request: Request):
    if AUTH_ENABLED and not active_portal_session(request):
        return redirect_login()
    username = signed_in_user(request)
    account = user_record(username)
    if not account:
        raise HTTPException(404, "Profile not found.")
    return templates.TemplateResponse(request, "profile.html", {
        "profile": account, "user": current_user(request), "signed_in_user": username,
        "is_admin": is_admin(request), "can_manage_users": can_manage_users(request),
        "impersonating": request.session.get("impersonating", "") if AUTH_ENABLED else "",
    })


@app.get("/profile/avatar")
async def profile_avatar(request: Request):
    """Serve only the signed-in user's private, validated avatar image."""
    username = signed_in_user(request)
    account = user_record(username)
    avatar_path = safe_avatar_path(str(account.get("avatar_filename") or "")) if account else None
    if not avatar_path:
        raise HTTPException(404, "Profile photo not found.")
    media_types = {".jpg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
    return FileResponse(avatar_path, media_type=media_types.get(avatar_path.suffix.lower(), "application/octet-stream"), headers={"Cache-Control": "private, no-store"})


@app.post("/profile")
async def update_profile(request: Request, email: str = Form(""), first_name: str = Form(""), last_name: str = Form(""), new_password: str = Form(""), confirm_new_password: str = Form(""), avatar_file: UploadFile | None = File(None), remove_avatar: str = Form("")):
    if AUTH_ENABLED and not active_portal_session(request):
        return redirect_login()
    username = signed_in_user(request)
    account = user_record(username)
    if not account:
        raise HTTPException(404, "Profile not found.")
    email, first_name, last_name = email.strip(), first_name.strip(), last_name.strip()
    if len(email) > 254 or len(first_name) > 80 or len(last_name) > 80:
        return RedirectResponse("/profile?error=Profile+fields+are+too+long", status_code=303)
    if email and ("@" not in email or email.startswith("@") or email.endswith("@")):
        return RedirectResponse("/profile?error=Enter+a+valid+email+address", status_code=303)
    if new_password:
        if len(new_password) < 12:
            return RedirectResponse("/profile?error=New+password+must+be+at+least+12+characters", status_code=303)
        if new_password != confirm_new_password:
            return RedirectResponse("/profile?error=New+passwords+do+not+match", status_code=303)
    elif confirm_new_password:
        return RedirectResponse("/profile?error=Enter+the+new+password+in+both+fields", status_code=303)
    new_avatar_filename: str | None = None
    if avatar_file and avatar_file.filename:
        avatar_data = bytearray()
        while chunk := await avatar_file.read(1024 * 1024):
            avatar_data.extend(chunk)
            if len(avatar_data) > MAX_AVATAR_BYTES:
                await avatar_file.close()
                return RedirectResponse("/profile?error=Profile+photo+must+be+2+MiB+or+smaller", status_code=303)
        await avatar_file.close()
        image_type = avatar_format(bytes(avatar_data))
        if not image_type:
            return RedirectResponse("/profile?error=Upload+a+JPG,+PNG,+or+WebP+profile+photo", status_code=303)
        suffix, _ = image_type
        new_avatar_filename = f"{uuid.uuid4().hex}{suffix}"
        try:
            (USER_AVATAR_DIR / new_avatar_filename).write_bytes(avatar_data)
        except OSError:
            portal_logger.exception("profile_avatar_write_failed username=%s", username)
            return RedirectResponse("/profile?error=The+portal+could+not+save+the+profile+photo", status_code=303)
    previous_avatar = str(account.get("avatar_filename") or "")
    avatar_filename = new_avatar_filename or (None if remove_avatar == "remove" else previous_avatar or None)
    with db.connect() as connection:
        if new_password:
            connection.execute("UPDATE portal_users SET email=?,first_name=?,last_name=?,avatar_filename=?,password_hash=? WHERE username=?", (email or None, first_name or None, last_name or None, avatar_filename, password_hash(new_password), username))
        else:
            connection.execute("UPDATE portal_users SET email=?,first_name=?,last_name=?,avatar_filename=? WHERE username=?", (email or None, first_name or None, last_name or None, avatar_filename, username))
    if previous_avatar and previous_avatar != avatar_filename:
        old_path = safe_avatar_path(previous_avatar)
        if old_path:
            old_path.unlink(missing_ok=True)
    audit(username, "update_profile", username, {"email_set": bool(email), "name_set": bool(first_name or last_name), "password_changed": bool(new_password), "avatar_changed": bool(new_avatar_filename or remove_avatar == "remove")})
    return RedirectResponse("/profile?message=Profile+updated", status_code=303)


@app.get("/admin/users", response_class=HTMLResponse)
async def admin_users(request: Request):
    require_permission(request, "users.manage", portal_wide=True)
    return templates.TemplateResponse(request, "admin_users.html", {
        "users": list_users(), "user": current_user(request), "signed_in_user": signed_in_user(request),
        "is_admin": True, "can_manage_users": True, "providers": {item["provider_id"]: item for item in auth.list_providers()}, "impersonating": request.session.get("impersonating", ""),
        "active_nav": "users", "has_admin_scope": has_admin_scope(request),
    })


def valid_role_keys(values: list[str]) -> set[str]:
    return {value for value in (item.strip() for item in values) if value in auth.ROLES}


@app.post("/admin/users")
async def admin_create_user(request: Request, username: str = Form(...), password: str = Form(...), roles: list[str] = Form([]), role: str = Form("")):
    require_permission(request, "users.manage", portal_wide=True)
    username = username.strip()
    # "role" is the v8.x single admin/user field, still accepted from scripts.
    chosen = valid_role_keys(roles) or ({auth.LEGACY_ROLE_MAP[role]} if role in auth.LEGACY_ROLE_MAP else {"lab_editor"})
    if not username or len(username) > 80 or any(char.isspace() or char in "\\/" for char in username) or len(password) < 12:
        return RedirectResponse("/admin/users?error=Use+a+username+without+spaces+or+slashes+and+a+password+of+at+least+12+characters", status_code=303)
    try:
        with db.connect() as connection:
            connection.execute("INSERT INTO portal_users(username,password_hash,role,enabled,created_at,auth_source) VALUES(?,?,?,?,?,?)", (username, password_hash(password), "user", 1, datetime.now(timezone.utc).isoformat(), "local"))
    except db.IntegrityError:
        return RedirectResponse("/admin/users?error=That+username+already+exists", status_code=303)
    auth.set_direct_roles(username, chosen)
    audit(audit_actor(request), "create_user", username, {"roles": sorted(chosen), "auth_source": "local"})
    return RedirectResponse("/admin/users?message=User+created", status_code=303)


@app.post("/admin/users/{username}/update")
async def admin_update_user(request: Request, username: str, roles: list[str] = Form([]), role: str = Form(""), enabled: str = Form("0"), password: str = Form(""), retained_backup_limit: int = Form(2)):
    require_permission(request, "users.manage", portal_wide=True)
    target = user_record(username)
    if not target:
        raise HTTPException(404, "User not found.")
    chosen = valid_role_keys(roles) or ({auth.LEGACY_ROLE_MAP[role]} if role in auth.LEGACY_ROLE_MAP else set())
    is_enabled = enabled == "1"
    if retained_backup_limit < 0 or retained_backup_limit > 100:
        return RedirectResponse("/admin/users?error=Retained+backup+limit+must+be+between+0+and+100", status_code=303)
    if target["username"].lower() == signed_in_user(request).lower() and not is_enabled:
        return RedirectResponse("/admin/users?error=You+cannot+disable+your+own+account", status_code=303)
    keeps_break_glass = target.get("auth_source") == "local" and is_enabled and "platform_admin" in chosen
    if not keeps_break_glass and "platform_admin" in auth.user_roles(target["username"]) and target.get("auth_source") == "local" and auth.enabled_local_admin_count(excluding=target["username"]) == 0:
        return RedirectResponse("/admin/users?error=At+least+one+enabled+local+Platform+Admin+must+remain+for+break-glass+access", status_code=303)
    if password and target.get("auth_source") != "local":
        return RedirectResponse("/admin/users?error=Directory+and+SSO+users+change+their+password+with+their+identity+provider", status_code=303)
    if password and len(password) < 12:
        return RedirectResponse("/admin/users?error=Passwords+must+be+at+least+12+characters", status_code=303)
    with db.connect() as connection:
        if password:
            connection.execute("UPDATE portal_users SET enabled=?,password_hash=?,retained_backup_limit=? WHERE username=?", (1 if is_enabled else 0, password_hash(password), retained_backup_limit, target["username"]))
        else:
            connection.execute("UPDATE portal_users SET enabled=?,retained_backup_limit=? WHERE username=?", (1 if is_enabled else 0, retained_backup_limit, target["username"]))
    auth.set_direct_roles(target["username"], chosen)
    audit(audit_actor(request), "update_user", target["username"], {"roles": sorted(chosen), "enabled": is_enabled, "password_reset": bool(password), "retained_backup_limit": retained_backup_limit})
    return RedirectResponse("/admin/users?message=User+updated", status_code=303)


def identity_redirect(path: str, *, message: str = "", error: str = "") -> RedirectResponse:
    query = f"?message={quote_plus(message)}" if message else (f"?error={quote_plus(error)}" if error else "")
    return RedirectResponse(f"{path}{query}", status_code=303)


@app.get("/admin/identity", response_class=HTMLResponse)
async def identity_providers_page(request: Request):
    require_permission(request, "users.manage", portal_wide=True)
    providers = auth.list_providers()
    counts = {item["provider_id"]: 0 for item in providers}
    for account in list_users():
        if account.get("auth_provider") in counts:
            counts[account["auth_provider"]] += 1
    return templates.TemplateResponse(request, "admin_identity.html", {
        **base_context(request, "identity"), "providers": providers, "user_counts": counts,
        "public_url": PORTAL_PUBLIC_URL, "presets": auth.SAML_PRESETS,
        "weak_secret_key": SECRET_KEY in {"change-this-before-production", "replace-with-a-long-random-value"} or len(SECRET_KEY) < 32,
    })


@app.get("/admin/identity/new/{kind}", response_class=HTMLResponse)
@app.get("/admin/identity/{provider_id}", response_class=HTMLResponse)
async def identity_provider_page(request: Request, provider_id: str = "", kind: str = ""):
    require_permission(request, "users.manage", portal_wide=True)
    provider = auth.get_provider(provider_id) if provider_id else None
    if provider_id and not provider:
        raise HTTPException(404, "Identity provider not found.")
    if not provider:
        if kind not in {"ldap", "saml"}:
            raise HTTPException(404, "Unknown provider type.")
        preset = request.query_params.get("preset", "entra")
        config = dict(auth.LDAP_DEFAULTS) if kind == "ldap" else {**auth.SAML_DEFAULTS, "preset": preset if preset in auth.SAML_PRESETS else "other"}
        provider = {"provider_id": "", "kind": kind, "name": "", "enabled": False, "config": config}
    groups: list[dict[str, str]] = []
    group_error = ""
    if provider["provider_id"] and provider["kind"] == "ldap" and "group_search" in request.query_params:
        try:
            groups = await asyncio.to_thread(auth.LdapDirectory(provider, secret_box).search_groups, request.query_params.get("group_search", ""))
        except auth.IdentityError as exc:
            group_error = str(exc)
    pid = provider["provider_id"]
    return templates.TemplateResponse(request, "admin_identity_edit.html", {
        **base_context(request, "identity"), "provider": provider, "config": provider["config"],
        "mappings": auth.group_mappings(pid) if pid else [], "presets": auth.SAML_PRESETS,
        "public_url": PORTAL_PUBLIC_URL, "groups": groups, "group_error": group_error,
        "group_search": request.query_params.get("group_search", ""),
        "cert_fingerprint": auth.fingerprint(str(provider["config"].get("idp_x509_cert") or "")) if provider["kind"] == "saml" else "",
        "sp_entity_id": f"{PORTAL_PUBLIC_URL}/auth/saml/{pid}/metadata" if pid and PORTAL_PUBLIC_URL else "",
        "sp_acs_url": f"{PORTAL_PUBLIC_URL}/auth/saml/{pid}/acs" if pid and PORTAL_PUBLIC_URL else "",
    })


@app.post("/admin/identity/save")
async def save_identity_provider(request: Request):
    require_permission(request, "users.manage", portal_wide=True)
    form = await request.form()
    value = lambda name, default="": str(form.get(name, default) or "").strip()
    kind, provider_id = value("kind"), value("provider_id").lower()
    existing = auth.get_provider(provider_id)
    back = f"/admin/identity/{provider_id}" if existing else f"/admin/identity/new/{kind}"
    config: dict[str, Any] = dict(existing["config"]) if existing else {}
    default_role = value("default_role")
    config["default_role"] = default_role if default_role in auth.ROLES else ""
    try:
        if kind == "ldap":
            auth.parse_ldaps_url(value("url"))
            config.update({
                "url": value("url"), "ca_pem": value("ca_pem"), "bind_dn": value("bind_dn"),
                "user_base_dn": value("user_base_dn"), "user_filter": value("user_filter") or auth.LDAP_DEFAULTS["user_filter"],
                "username_attribute": value("username_attribute") or "sAMAccountName", "group_base_dn": value("group_base_dn"),
                "group_filter": value("group_filter") or auth.LDAP_DEFAULTS["group_filter"],
                "nested_groups": value("nested_groups") == "1", "timeout_seconds": int(value("timeout_seconds") or 10),
            })
            if "{username}" not in config["user_filter"]:
                raise auth.IdentityError("The user filter must contain {username}.")
            if value("bind_password"):
                config["bind_password_enc"] = secret_box.encrypt(value("bind_password"))
            if not config["user_base_dn"] or not config["bind_dn"] or not config.get("bind_password_enc"):
                raise auth.IdentityError("Enter the user search base, the service account DN and its password.")
            auth.ldap_tls(config["ca_pem"])
        elif kind == "saml":
            preset = value("preset") if value("preset") in auth.SAML_PRESETS else "other"
            config["preset"] = preset
            if value("idp_metadata_xml"):
                config.update(auth.parse_idp_metadata(value("idp_metadata_xml")))
            else:
                config.update({"idp_entity_id": value("idp_entity_id"), "idp_sso_url": value("idp_sso_url"), "idp_x509_cert": value("idp_x509_cert")})
            for key in ("username_attribute", "email_attribute", "first_name_attribute", "last_name_attribute", "groups_attribute"):
                config[key] = value(key)
            if not config["idp_entity_id"] or not config["idp_sso_url"].startswith("https://") or not config["idp_x509_cert"]:
                raise auth.IdentityError("Paste the IdP metadata, or enter its entity ID, an https:// sign-in URL and its signing certificate.")
        else:
            raise auth.IdentityError("Unknown provider type.")
        auth.save_provider(provider_id, kind, value("name"), value("enabled") == "1", config)
    except (auth.IdentityError, ValueError) as exc:
        return identity_redirect(back, error=str(exc) if isinstance(exc, auth.IdentityError) else "Check the numeric fields.")
    audit(audit_actor(request), "save_identity_provider", provider_id, {"kind": kind, "enabled": value("enabled") == "1", "created": not existing, "bind_password_changed": bool(value("bind_password"))})
    return identity_redirect(f"/admin/identity/{provider_id}", message="Identity provider saved")


@app.post("/admin/identity/{provider_id}/test")
async def test_identity_provider(request: Request, provider_id: str, sample_user: str = Form("")):
    require_permission(request, "users.manage", portal_wide=True)
    provider = auth.get_provider(provider_id)
    if not provider or provider["kind"] != "ldap":
        raise HTTPException(404, "Directory not found.")
    try:
        results = await asyncio.to_thread(auth.LdapDirectory(provider, secret_box).test, sample_user.strip())
    except auth.IdentityError as exc:
        audit(audit_actor(request), "test_identity_provider", provider_id, {"result": "failed"}, outcome="failure")
        return identity_redirect(f"/admin/identity/{provider_id}", error=f"Test failed: {exc}")
    audit(audit_actor(request), "test_identity_provider", provider_id, {"result": "passed"})
    return identity_redirect(f"/admin/identity/{provider_id}", message=" ".join(results))


@app.post("/admin/identity/{provider_id}/delete")
async def delete_identity_provider(request: Request, provider_id: str, confirmation: str = Form("")):
    require_permission(request, "users.manage", portal_wide=True)
    if not auth.get_provider(provider_id):
        raise HTTPException(404, "Identity provider not found.")
    if confirmation.strip() != provider_id:
        return identity_redirect(f"/admin/identity/{provider_id}", error="Type the provider ID to confirm deletion.")
    auth.delete_provider(provider_id)
    audit(audit_actor(request), "delete_identity_provider", provider_id)
    return identity_redirect("/admin/identity", message="Identity provider deleted. Its users can no longer sign in.")


@app.post("/admin/identity/{provider_id}/mappings")
async def add_identity_mapping(request: Request, provider_id: str, group_id: str = Form(...), group_name: str = Form(""), role_key: str = Form(...)):
    require_permission(request, "users.manage", portal_wide=True)
    if not auth.get_provider(provider_id):
        raise HTTPException(404, "Identity provider not found.")
    try:
        auth.add_group_mapping(provider_id, group_id, group_name, role_key, audit_actor(request))
    except auth.IdentityError as exc:
        return identity_redirect(f"/admin/identity/{provider_id}", error=str(exc))
    audit(audit_actor(request), "map_group_role", provider_id, {"group_id": group_id.strip(), "role": role_key})
    return identity_redirect(f"/admin/identity/{provider_id}", message="Group mapped. Members get the role at their next sign-in.")


@app.post("/admin/identity/{provider_id}/mappings/remove")
async def remove_identity_mapping(request: Request, provider_id: str, group_id: str = Form(...), role_key: str = Form(...)):
    require_permission(request, "users.manage", portal_wide=True)
    auth.remove_group_mapping(provider_id, group_id, role_key)
    audit(audit_actor(request), "unmap_group_role", provider_id, {"group_id": group_id, "role": role_key})
    return identity_redirect(f"/admin/identity/{provider_id}", message="Mapping removed. It takes effect at each member's next sign-in.")


@app.get("/admin/roles", response_class=HTMLResponse)
async def roles_page(request: Request):
    require_permission(request, "users.manage", portal_wide=True)
    users = list_users()
    holders = {key: sum(1 for account in users if key in account["direct_roles"] or key in account["group_roles"]) for key in auth.ROLES}
    return templates.TemplateResponse(request, "admin_roles.html", {**base_context(request, "roles"), "holders": holders, "mappings": auth.group_mappings(), "providers": {item["provider_id"]: item for item in auth.list_providers()}})


@app.post("/admin/impersonate/{username}")
async def impersonate_user(request: Request, username: str):
    require_admin(request)
    target = user_record(username)
    if not target or not target.get("enabled"):
        raise HTTPException(404, "Enabled user not found.")
    request.session["impersonating"] = target["username"]
    audit(signed_in_user(request), "start_impersonation", target["username"])
    return RedirectResponse("/", status_code=303)


@app.post("/admin/stop-impersonation")
async def stop_impersonation(request: Request):
    require_admin(request)
    target = request.session.pop("impersonating", "")
    if target:
        audit(signed_in_user(request), "stop_impersonation", str(target))
    return RedirectResponse("/", status_code=303)


@app.post("/admin/labs/{project_id}/owner")
async def admin_set_lab_owner(request: Request, project_id: str, username: str = Form(...)):
    require_permission(request, "labs.all", portal_wide=True)
    target = user_record(username)
    if not target or not target.get("enabled"):
        return RedirectResponse("/?error=Choose+an+enabled+portal+user", status_code=303)
    set_lab_owner(project_id, target["username"])
    audit(audit_actor(request), "assign_lab_owner", project_id, {"owner": target["username"]})
    return RedirectResponse("/?message=Lab+owner+updated", status_code=303)


@app.post("/projects/{project_id}/shares")
async def share_lab(request: Request, project_id: str, username: str = Form(...), return_to: str = Form("")):
    require_permission(request, "labs.edit")
    require_project_access(request, project_id)
    if not can_manage_lab_sharing(request, project_id):
        raise HTTPException(403, "Only the lab owner or an Administrator can share this lab.")
    target = user_record(username)
    owner = lab_owner(project_id)
    if not target or not target.get("enabled") or target["username"] == owner:
        return redirect_back(return_to, project_id, "error=Choose+another+enabled+portal+user")
    with db.connect() as connection:
        connection.execute("INSERT INTO lab_shares(project_id,username,shared_by,shared_at) VALUES(?,?,?,?) ON CONFLICT(project_id,username) DO UPDATE SET shared_by=excluded.shared_by, shared_at=excluded.shared_at", (project_id, target["username"], audit_actor(request), datetime.now(timezone.utc).isoformat()))
    audit(audit_actor(request), "share_lab", project_id, {"shared_with": target["username"]})
    return redirect_back(return_to, project_id, "message=Lab+shared")


@app.post("/projects/{project_id}/shares/{username}/remove")
async def remove_lab_share(request: Request, project_id: str, username: str, return_to: str = Form("")):
    require_permission(request, "labs.edit")
    require_project_access(request, project_id)
    if not can_manage_lab_sharing(request, project_id):
        raise HTTPException(403, "Only the lab owner or an Administrator can change sharing.")
    with db.connect() as connection:
        connection.execute("DELETE FROM lab_shares WHERE project_id=? AND username=?", (project_id, username))
    audit(audit_actor(request), "remove_lab_share", project_id, {"user": username})
    return redirect_back(return_to, project_id, "message=Lab+sharing+updated")


@app.post("/projects/{project_id}/snapshots/{snapshot_id}/shares")
async def share_backup(request: Request, project_id: str, snapshot_id: str, username: str = Form(...)):
    require_permission(request, "labs.edit")
    require_project_access(request, project_id)
    if not can_manage_lab_sharing(request, project_id):
        raise HTTPException(403, "Only the lab owner or an Administrator can share a backup.")
    target = user_record(username)
    if not target or not target.get("enabled") or target["username"] == lab_owner(project_id):
        return RedirectResponse(f"/projects/{project_id}?error=Choose+another+enabled+portal+user", status_code=303)
    snapshots = await gns3("GET", f"/v2/projects/{project_id}/snapshots")
    if not any(str(item.get("snapshot_id")) == snapshot_id for item in snapshots):
        raise HTTPException(404, "Backup not found.")
    with db.connect() as connection:
        connection.execute("INSERT INTO backup_shares(project_id,snapshot_id,username,shared_by,shared_at) VALUES(?,?,?,?,?) ON CONFLICT(project_id,snapshot_id,username) DO UPDATE SET shared_by=excluded.shared_by, shared_at=excluded.shared_at", (project_id, snapshot_id, target["username"], audit_actor(request), datetime.now(timezone.utc).isoformat()))
    audit(audit_actor(request), "share_backup", project_id, {"snapshot_id": snapshot_id, "shared_with": target["username"]})
    return RedirectResponse(f"/projects/{project_id}?message=Backup+shared", status_code=303)


@app.post("/projects/{project_id}/snapshots/{snapshot_id}/shares/{username}/remove")
async def remove_backup_share(request: Request, project_id: str, snapshot_id: str, username: str):
    require_project_access(request, project_id)
    if not can_manage_lab_sharing(request, project_id):
        raise HTTPException(403, "Only the lab owner or an Administrator can change backup sharing.")
    with db.connect() as connection:
        connection.execute("DELETE FROM backup_shares WHERE project_id=? AND snapshot_id=? AND username=?", (project_id, snapshot_id, username))
    audit(audit_actor(request), "remove_backup_share", project_id, {"snapshot_id": snapshot_id, "user": username})
    return RedirectResponse(f"/projects/{project_id}?message=Backup+sharing+updated", status_code=303)


@app.post("/projects/{project_id}/snapshots/{snapshot_id}/template")
async def create_lab_template(request: Request, project_id: str, snapshot_id: str, name: str = Form(...), description: str = Form("")):
    require_permission(request, "labs.edit")
    require_project_access(request, project_id)
    if not can_manage_lab_sharing(request, project_id):
        raise HTTPException(403, "Only the lab owner or an Administrator can create a template.")
    name, description = name.strip(), description.strip()
    if not name or len(name) > 100 or len(description) > 500:
        return RedirectResponse(f"/projects/{project_id}?error=Use+a+template+name+of+1+to+100+characters", status_code=303)
    snapshots = await gns3("GET", f"/v2/projects/{project_id}/snapshots")
    snapshot = next((item for item in snapshots if str(item.get("snapshot_id")) == snapshot_id), None)
    if not snapshot:
        raise HTTPException(404, "Backup not found.")
    template_id = str(uuid.uuid4())
    source = local_snapshot_file(await gns3("GET", f"/v2/projects/{project_id}"), snapshot)
    if not source:
        return RedirectResponse(f"/projects/{project_id}?error=This+backup+cannot+be+read+locally.+Configure+snapshot+storage+access+first", status_code=303)
    template_dir = LAB_TEMPLATE_DIR / template_id
    destination = template_dir / "template.gns3snapshot"
    try:
        template_dir.mkdir(parents=True, exist_ok=False)
        await asyncio.to_thread(shutil.copy2, source, destination)
        if destination.stat().st_size != source.stat().st_size:
            raise OSError("Template archive size verification failed")
        with db.connect() as connection:
            status_value = "approved" if admin_permission(request, "templates.approve") else "pending"
            approved_by = audit_actor(request) if status_value == "approved" else None
            approved_at = datetime.now(timezone.utc).isoformat() if status_value == "approved" else None
            connection.execute("INSERT INTO lab_templates(template_id,owner,source_project_id,snapshot_id,name,description,archive_path,size_bytes,created_at,approval_status,approved_by,approved_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (template_id, current_user(request), project_id, snapshot_id, name, description or None, str(destination), destination.stat().st_size, datetime.now(timezone.utc).isoformat(), status_value, approved_by, approved_at))
    except db.IntegrityError:
        shutil.rmtree(template_dir, ignore_errors=True)
        return RedirectResponse(f"/projects/{project_id}?error=This+backup+is+already+a+template", status_code=303)
    except OSError:
        shutil.rmtree(template_dir, ignore_errors=True)
        return RedirectResponse(f"/projects/{project_id}?error=The+template+archive+could+not+be+created", status_code=303)
    audit(audit_actor(request), "create_lab_template", project_id, {"template_id": template_id, "snapshot_id": snapshot_id, "name": name, "approval_status": status_value})
    message = "Lab+template+created" if status_value == "approved" else "Template+submitted+for+Administrator+approval"
    return RedirectResponse(f"/projects/{project_id}?message={message}", status_code=303)


@app.post("/templates/{template_id}/access")
async def grant_template_access(request: Request, template_id: str, username: str = Form(...)):
    template = template_record(template_id)
    if not template or (not has_admin_scope(request) and not admin_permission(request, "templates.approve") and template["owner"] != current_user(request)):
        raise HTTPException(404, "Lab template not found.")
    target = user_record(username.strip())
    if not target or not target.get("enabled"):
        return RedirectResponse(f"/projects/{template['source_project_id']}?error=Choose+an+enabled+portal+user", status_code=303)
    with db.connect() as connection:
        connection.execute("INSERT INTO lab_template_access(template_id,subject_type,subject_id,granted_by,granted_at) VALUES(?,?,?,?,?) ON CONFLICT(template_id,subject_type,subject_id) DO UPDATE SET granted_by=excluded.granted_by,granted_at=excluded.granted_at", (template_id, "local_user", target["username"], audit_actor(request), datetime.now(timezone.utc).isoformat()))
    audit(audit_actor(request), "grant_lab_template", template["source_project_id"], {"template_id": template_id, "username": target["username"]})
    return RedirectResponse(f"/projects/{template['source_project_id']}?message=Template+shared", status_code=303)


@app.post("/templates/{template_id}/access/{username}/remove")
async def revoke_template_access(request: Request, template_id: str, username: str):
    template = template_record(template_id)
    if not template or (not has_admin_scope(request) and not admin_permission(request, "templates.approve") and template["owner"] != current_user(request)):
        raise HTTPException(404, "Lab template not found.")
    with db.connect() as connection:
        connection.execute("DELETE FROM lab_template_access WHERE template_id=? AND subject_type='local_user' AND subject_id=?", (template_id, username))
    audit(audit_actor(request), "revoke_lab_template", template["source_project_id"], {"template_id": template_id, "username": username})
    return RedirectResponse(f"/projects/{template['source_project_id']}?message=Template+access+updated", status_code=303)


@app.post("/templates/{template_id}/unpublish")
async def unpublish_template(request: Request, template_id: str):
    template = template_record(template_id)
    if not template or (not has_admin_scope(request) and not admin_permission(request, "templates.approve") and template["owner"] != current_user(request)):
        raise HTTPException(404, "Lab template not found.")
    with db.connect() as connection:
        connection.execute("DELETE FROM lab_template_access WHERE template_id=?", (template_id,))
        connection.execute("DELETE FROM lab_templates WHERE template_id=?", (template_id,))
    archive_path = Path(str(template.get("archive_path") or ""))
    try:
        archive_path.resolve(strict=True).relative_to(LAB_TEMPLATE_DIR.resolve(strict=True))
        shutil.rmtree(archive_path.parent, ignore_errors=True)
    except (OSError, ValueError):
        pass
    audit(audit_actor(request), "unpublish_lab_template", template["source_project_id"], {"template_id": template_id, "name": template["name"]})
    return RedirectResponse("/templates?message=Lab+template+unpublished", status_code=303)


@app.post("/templates/{template_id}/approve")
async def approve_template(request: Request, template_id: str):
    require_permission(request, "templates.approve", portal_wide=True)
    template = template_record(template_id)
    if not template:
        raise HTTPException(404, "Lab template not found.")
    if template.get("approval_status") == "approved":
        return RedirectResponse("/templates?message=Lab+template+is+already+approved", status_code=303)
    with db.connect() as connection:
        connection.execute("UPDATE lab_templates SET approval_status='approved',approved_by=?,approved_at=? WHERE template_id=?", (audit_actor(request), datetime.now(timezone.utc).isoformat(), template_id))
    audit(audit_actor(request), "approve_lab_template", template["source_project_id"], {"template_id": template_id, "name": template["name"], "owner": template["owner"]})
    return RedirectResponse("/templates?message=Lab+template+approved+and+published", status_code=303)


def base_context(request: Request, active_nav: str = "") -> dict[str, Any]:
    """Values every page in the console layout needs."""
    return {
        "user": current_user(request), "signed_in_user": signed_in_user(request),
        "is_admin": is_admin(request), "can_manage_users": can_manage_users(request),
        "has_admin_scope": has_admin_scope(request), "active_nav": active_nav,
        "impersonating": request.session.get("impersonating", "") if AUTH_ENABLED else "",
    }


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    if AUTH_ENABLED and not active_portal_session(request):
        return redirect_login()
    context = await lab_overview(request)
    if not isinstance(context, dict):
        return context
    return templates.TemplateResponse(request, "dashboard.html", {**context, "active_nav": "labs"})


@app.get("/labs/new", response_class=HTMLResponse)
async def new_lab_page(request: Request):
    if AUTH_ENABLED and not active_portal_session(request):
        return redirect_login()
    require_permission(request, "labs.create")
    context = await lab_overview(request)
    if not isinstance(context, dict):
        return context
    return templates.TemplateResponse(request, "new_lab.html", {**context, "active_nav": "new_lab"})


@app.get("/templates", response_class=HTMLResponse)
async def templates_page(request: Request):
    if AUTH_ENABLED and not active_portal_session(request):
        return redirect_login()
    context = await lab_overview(request)
    if not isinstance(context, dict):
        return context
    return templates.TemplateResponse(request, "templates.html", {**context, "active_nav": "templates"})


@app.get("/activity", response_class=HTMLResponse)
async def activity_page(request: Request):
    if AUTH_ENABLED and not active_portal_session(request):
        return redirect_login()
    context = base_context(request, "activity")
    context["audit"] = recent_audit(limit=500, username=current_user(request), include_all=admin_permission(request, "audit.all"))
    context["audit_all"] = admin_permission(request, "audit.all")
    return templates.TemplateResponse(request, "activity.html", context)


@app.get("/admin/status", response_class=HTMLResponse)
async def server_status_page(request: Request):
    if AUTH_ENABLED and not active_portal_session(request):
        return redirect_login()
    require_permission(request, "servers.view", portal_wide=True)
    return templates.TemplateResponse(request, "server_status.html", base_context(request, "status"))


async def lab_overview(request: Request) -> dict[str, Any] | HTMLResponse:
    """Collect labs, backups and templates for the labs, new-lab and templates pages."""
    try:
        projects = await gns3("GET", "/v2/projects")
        # Keep the dashboard stable: older labs stay at the top and each new
        # lab is added as the last row. GNS3 returns ISO-8601 created_at values.
        projects.sort(key=lambda project: str(project.get("created_at") or ""))
        if AUTH_ENABLED and not has_admin_scope(request):
            allowed = owned_project_ids(current_user(request)) | shared_project_ids(current_user(request))
            projects = [project for project in projects if project["project_id"] in allowed]
        snapshot_lists, node_lists = await asyncio.gather(
            asyncio.gather(*(gns3("GET", f"/v2/projects/{project['project_id']}/snapshots") for project in projects)),
            asyncio.gather(*(gns3("GET", f"/v2/projects/{project['project_id']}/nodes") for project in projects)),
        )
    except HTTPException as exc:
        return error_page(request, exc)
    import_sources = [
        {"project_id": project["project_id"], "project_name": project.get("name", "Unnamed lab"), "snapshots": snapshots}
        for project, snapshots in zip(projects, snapshot_lists)
        if snapshots
    ]
    shared_backup_sources: list[dict[str, Any]] = []
    if AUTH_ENABLED and not has_admin_scope(request):
        for shared in backup_shares_for(current_user(request)):
            try:
                source_project = await gns3("GET", f"/v2/projects/{shared['project_id']}")
                source_snapshots = await gns3("GET", f"/v2/projects/{shared['project_id']}/snapshots")
            except HTTPException:
                continue
            snapshot = next((item for item in source_snapshots if str(item.get("snapshot_id")) == shared["snapshot_id"]), None)
            if snapshot:
                shared_backup_sources.append({"project_id": shared["project_id"], "snapshot_id": shared["snapshot_id"], "project_name": source_project.get("name", "Shared lab"), "snapshot_name": snapshot.get("name", "Unnamed backup")})
    available_templates: list[dict[str, Any]] = []
    pending_templates: list[dict[str, Any]] = []
    if AUTH_ENABLED:
        templates_to_show = approved_templates()
        pending_source = templates_for_user(current_user(request), include_owned=has_admin_scope(request) or admin_permission(request, "templates.approve"))
        for template in templates_to_show:
            archive_path = Path(str(template.get("archive_path") or ""))
            try:
                archive_path.resolve(strict=True).relative_to(LAB_TEMPLATE_DIR.resolve(strict=True))
            except (OSError, ValueError):
                continue
            available_templates.append({**template, "access_users": template_access_users(template["template_id"])})
        for template in pending_source:
            if template.get("approval_status") != "pending":
                continue
            archive_path = Path(str(template.get("archive_path") or ""))
            try:
                archive_path.resolve(strict=True).relative_to(LAB_TEMPLATE_DIR.resolve(strict=True))
            except (OSError, ValueError):
                continue
            pending_templates.append(template)
    # The dashboard action is deliberately derived from node state, rather
    # than project open/closed state. Built-in Cloud and Ethernet switch nodes
    # are always reported as started by GNS3 and are not lab workloads.
    for project, nodes, snapshots in zip(projects, node_lists, snapshot_lists):
        workload_nodes = [node for node in nodes if not is_non_stoppable_node(node)]
        project["node_total"] = len(workload_nodes)
        project["node_running"] = sum(1 for node in workload_nodes if node.get("status") != "stopped")
        project["is_running"] = project["node_running"] > 0
        project["backup_count"] = len(snapshots)
        backup_times = [format_backup_datetime(item.get("created_at")) for item in snapshots if item.get("created_at")]
        project["last_backup_at"] = max((value for value in backup_times if value[:1].isdigit()), default="")
        project["access_lab_url"] = gns3_webui_project_url(project["project_id"])
        project["owner"] = lab_owner(project["project_id"]) or "Unassigned"
        project["shared_with"] = shared_users(project["project_id"])
        project["can_share"] = can_manage_lab_sharing(request, project["project_id"])
        project["lab_group"] = "user" if has_admin_scope(request) and project["owner"] not in {current_user(request), "Unassigned"} else "mine"
    return {
        "projects": projects, "import_sources": import_sources, "shared_backup_sources": shared_backup_sources, "available_templates": available_templates, "pending_templates": pending_templates, "user": current_user(request),
        "signed_in_user": signed_in_user(request), "is_admin": is_admin(request), "can_manage_users": can_manage_users(request),
        "impersonating": request.session.get("impersonating", "") if AUTH_ENABLED else "",
        "portal_users": [account for account in list_users() if account.get("enabled")],
        "my_lab_count": sum(1 for project in projects if project["lab_group"] == "mine"),
        "user_lab_count": sum(1 for project in projects if project["lab_group"] == "user"),
        "retained_backups": retained_backups_for(None if has_admin_scope(request) else current_user(request)),
        "gns3_url": GNS3_URL, "has_admin_scope": has_admin_scope(request),
        "import_job_id": request.query_params.get("import_job", ""),
        "running_lab_count": sum(1 for project in projects if project["is_running"]),
        "running_node_count": sum(project["node_running"] for project in projects),
        "unassigned_lab_count": sum(1 for project in projects if project["owner"] == "Unassigned"),
    }


@app.get("/admin/system-status")
async def administrator_system_status(request: Request):
    require_permission(request, "servers.view", portal_wide=True)
    try:
        computes = await gns3("GET", "/v2/computes")
        if not isinstance(computes, list) or not computes:
            return {"available": False, "message": "No GNS3 compute is registered.", "disks": {}}
        compute = next((item for item in computes if str(item.get("compute_id", item.get("id", ""))) == GNS3_MONITOR_COMPUTE_ID), None) if GNS3_MONITOR_COMPUTE_ID else computes[0]
        compute_id = str(compute.get("compute_id", compute.get("id", ""))) if isinstance(compute, dict) else ""
        if compute_id:
            compute = await gns3("GET", f"/v2/computes/{quote(compute_id, safe='')}")
    except HTTPException as exc:
        return {"available": False, "message": str(exc.detail), "disks": {}}
    if not isinstance(compute, dict):
        return {"available": False, "message": "GNS3 returned an unexpected system-status response.", "disks": {}}
    cpu_percent = numeric_value(compute, "cpu_usage", "cpu_usage_percent", "cpu_percent")
    memory_total = numeric_value(compute, "memory_total", "memory_total_bytes")
    memory_used = numeric_value(compute, "memory_usage", "memory_used", "memory_used_bytes")
    if memory_total and memory_used is not None and memory_used <= 100 and memory_total > 1024:
        memory_used = memory_total * memory_used / 100
    # GNS3 v2 computes report memory only as memory_usage_percent, without totals.
    memory_percent = numeric_value(compute, "memory_usage_percent", "memory_percent")
    if memory_percent is None and memory_total and memory_used is not None:
        memory_percent = memory_used / memory_total * 100
    now = perf_counter(); current = disk_io_counters(PORTAL_MONITOR_DISKS); previous = app.state.monitor_disk_samples
    disks: dict[str, dict[str, float | int | None]] = {}
    for device, values in current.items():
        old = previous.get(device); elapsed = max(0.001, now - float(old.get("sampled_at", now))) if old else 0.0
        disks[device] = {**values, "read_bytes_per_second": max(0.0, (values["read_bytes"] - int(old["read_bytes"])) / elapsed) if old else None, "write_bytes_per_second": max(0.0, (values["write_bytes"] - int(old["write_bytes"])) / elapsed) if old else None}
    app.state.monitor_disk_samples = {device: {**values, "sampled_at": now} for device, values in current.items()}
    available = cpu_percent is not None or memory_percent is not None
    return {"available": available, "cpu_percent": cpu_percent, "memory_percent": memory_percent, "memory_total_bytes": int(memory_total) if memory_total is not None else None, "memory_used_bytes": int(memory_used) if memory_used is not None else None, "disks": disks, "message": "" if available else "This GNS3 server did not provide CPU or memory status."}


@app.post("/projects")
async def create_project(request: Request, name: str = Form(...)):
    require_permission(request, "labs.create")
    user = current_user(request)
    name = name.strip()
    if not name or len(name) > 80:
        return RedirectResponse("/?error=Use+a+lab+name+between+1+and+80+characters", status_code=303)
    project = await gns3("POST", "/v2/projects", json={"name": name})
    set_lab_owner(project["project_id"], user)
    linked, link_reason = await asyncio.to_thread(
        ensure_hdd_snapshot_link,
        await gns3("GET", f"/v2/projects/{project['project_id']}"),
    )
    if linked:
        audit(audit_actor(request), "link_lab_snapshots_to_hdd", project["project_id"], {"reason": link_reason})
    else:
        portal_logger.warning("snapshot_hdd_link_skipped project_id=%s reason=%s", project["project_id"], link_reason)
    audit(audit_actor(request), "create_lab", project["project_id"], {"name": name, "owner": user})
    return RedirectResponse(f"/projects/{project['project_id']}", status_code=303)


def redirect_back(return_to: str, project_id: str, query: str = "") -> RedirectResponse:
    """Redirect to the lab page a form came from, or to the labs list.

    Only this lab's own page is accepted, so the field cannot be used as an
    open redirect.
    """
    path, _, fragment = (return_to or "").partition("#")
    target = path if path == f"/projects/{project_id}" else "/"
    url = target + (f"?{query}" if query else "")
    if target != "/" and fragment.isalpha():
        url += f"#{fragment}"
    return RedirectResponse(url, status_code=303)


@app.post("/projects/{project_id}/start")
async def start_lab(request: Request, project_id: str, return_to: str = Form("")):
    require_permission(request, "labs.operate")
    user = current_user(request)
    require_project_access(request, project_id)
    if backup_is_active(project_id):
        return redirect_back(return_to, project_id, "error=A+backup+is+running+for+this+lab.+Wait+for+it+to+finish+before+starting+nodes")
    try:
        started, ignored = await set_lab_node_power(project_id, "start")
    except HTTPException as exc:
        return redirect_back(return_to, project_id, f"error={quote_plus(str(exc.detail))}")
    audit(audit_actor(request), "start_lab", project_id, {"nodes_started": started, "non_stoppable_nodes": ignored})
    return redirect_back(return_to, project_id, f"message={quote_plus(f'Start requested for {started} lab node(s).')}")


@app.post("/projects/{project_id}/stop")
async def stop_lab(request: Request, project_id: str, return_to: str = Form("")):
    require_permission(request, "labs.operate")
    user = current_user(request)
    require_project_access(request, project_id)
    if backup_is_active(project_id):
        return redirect_back(return_to, project_id, "error=A+backup+is+running+for+this+lab.+Wait+for+it+to+finish+before+stopping+nodes")
    try:
        stopped, ignored = await set_lab_node_power(project_id, "stop")
    except HTTPException as exc:
        return redirect_back(return_to, project_id, f"error={quote_plus(str(exc.detail))}")
    audit(audit_actor(request), "stop_lab", project_id, {"nodes_stopped": stopped, "non_stoppable_nodes": ignored})
    return redirect_back(return_to, project_id, f"message={quote_plus(f'Stop requested for {stopped} lab node(s).')}")


@app.get("/projects/{project_id}", response_class=HTMLResponse)
async def project_page(request: Request, project_id: str):
    if AUTH_ENABLED and not active_portal_session(request):
        return redirect_login()
    try:
        require_project_access(request, project_id)
        project = await gns3("GET", f"/v2/projects/{project_id}")
        snapshots, nodes = await asyncio.gather(
            gns3("GET", f"/v2/projects/{project_id}/snapshots"),
            gns3("GET", f"/v2/projects/{project_id}/nodes"),
        )
    except HTTPException as exc:
        return error_page(request, exc)
    snapshots, snapshot_total_bytes, storage_scanned = snapshot_storage(project, snapshots)
    nodes = sorted(nodes or [], key=lambda node: str(node.get("name", "")).lower())
    node_running = sum(1 for node in nodes if not is_non_stoppable_node(node) and node.get("status") != "stopped")
    snapshots.sort(key=lambda item: str(item.get("completed_at") or item.get("created_at") or ""))
    requested_job_id = request.query_params.get("backup_job", "")
    backup_job_id = requested_job_id if requested_job_id in app.state.backup_jobs else ""
    if not backup_job_id:
        backup_job_id = next((job_id for job_id, job in app.state.backup_jobs.items()
                              if job.get("project_id") == project_id and job.get("status") in ACTIVE_BACKUP_STATES), "")
    return templates.TemplateResponse(request, "project.html", {
        "project": project, "snapshots": snapshots, "snapshot_total_bytes": snapshot_total_bytes,
        "nodes": nodes, "node_running": node_running, "node_total": sum(1 for node in nodes if not is_non_stoppable_node(node)),
        "access_lab_url": gns3_webui_project_url(project_id), "active_nav": "labs", "lab_shared_with": shared_users(project_id), "has_admin_scope": has_admin_scope(request),
        "storage_scanned": storage_scanned, "user": current_user(request), "gns3_url": GNS3_URL,
        "signed_in_user": signed_in_user(request), "is_admin": is_admin(request), "can_manage_users": can_manage_users(request),
        "can_share": can_manage_lab_sharing(request, project_id), "lab_owner": lab_owner(project_id), "portal_users": [account for account in list_users() if account.get("enabled")],
        "snapshot_shared_with": {str(item.get("snapshot_id")): backup_shared_users(project_id, str(item.get("snapshot_id"))) for item in snapshots},
        "snapshot_templates": {str(item["snapshot_id"]): item for item in templates_for_user(current_user(request), include_owned=has_admin_scope(request)) if item["source_project_id"] == project_id},
        "template_access": {item["template_id"]: template_access_users(item["template_id"]) for item in templates_for_user(current_user(request), include_owned=has_admin_scope(request)) if item["source_project_id"] == project_id},
        "retained_remaining": max(0, retained_backup_limit(current_user(request)) - retained_backup_count(current_user(request))),
        "retained_limit": retained_backup_limit(current_user(request)),
        "impersonating": request.session.get("impersonating", "") if AUTH_ENABLED else "",
        "backup_job_id": backup_job_id,
        "import_job_id": request.query_params.get("import_job", ""),
    })


@app.post("/projects/{project_id}/snapshots")
async def create_snapshot(request: Request, project_id: str, name: str = Form(...), stop_confirmation: str = Form("")):
    require_permission(request, "labs.edit")
    user = current_user(request)
    require_project_access(request, project_id)
    name = name.strip()
    if not name:
        return RedirectResponse(f"/projects/{project_id}?error=Snapshot+name+is+required", status_code=303)
    if stop_confirmation != "stop-and-backup":
        return RedirectResponse(f"/projects/{project_id}?error=Confirm+that+all+nodes+will+be+stopped+before+backup", status_code=303)
    if backup_is_active(project_id):
        return RedirectResponse(f"/projects/{project_id}?error=A+backup+is+already+running+for+this+lab", status_code=303)
    job_id = str(uuid.uuid4())
    job = {"project_id": project_id, "name": name, "actor": user, "audit_actor": audit_actor(request), "status": "queued", "progress_percent": 0, "progress_is_estimated": False, "message": "Backup queued…", "snapshot_id": None, "cancel_requested": False, "created_at": datetime.now(timezone.utc).isoformat(), "completed_at": None}
    app.state.backup_jobs[job_id] = job
    asyncio.create_task(run_backup_job(job))
    return RedirectResponse(f"/projects/{project_id}?backup_job={job_id}", status_code=303)


@app.get("/projects/{project_id}/backup-jobs/{job_id}")
async def backup_job_status(request: Request, project_id: str, job_id: str):
    current_user(request)
    require_project_access(request, project_id)
    job = app.state.backup_jobs.get(job_id)
    if not job or job.get("project_id") != project_id:
        raise HTTPException(404, "Backup job not found. It may have been cleared after a portal restart.")
    return JSONResponse({key: job.get(key) for key in ("status", "progress_percent", "progress_is_estimated", "message", "snapshot_id", "name", "cancel_requested", "created_at", "completed_at")})


@app.post("/projects/{project_id}/backup-jobs/{job_id}/cancel")
async def cancel_backup_job(request: Request, project_id: str, job_id: str):
    """Request safe cancellation without tearing down a controller operation."""
    user = current_user(request)
    require_project_access(request, project_id)
    job = app.state.backup_jobs.get(job_id)
    if not job or job.get("project_id") != project_id:
        raise HTTPException(404, "Backup job not found.")
    if job.get("status") not in ACTIVE_BACKUP_STATES:
        raise HTTPException(409, "This backup is no longer running.")
    job["cancel_requested"] = True
    if job.get("status") == "creating_snapshot":
        job.update(status="cancelling", message="Cancellation requested. GNS3 must finish its current write safely; the resulting backup will be removed.")
    else:
        job.update(message="Cancellation requested. Stopping before the snapshot is created…")
    portal_logger.info("backup_cancel_requested project_id=%s job_id=%s actor=%s status=%s", project_id, job_id, user, job.get("status"))
    audit(audit_actor(request), "cancel_backup", project_id, {"job_id": job_id, "name": job.get("name")})
    return JSONResponse({"ok": True})


@app.get("/projects/{project_id}/export")
async def export_project(request: Request, project_id: str):
    """Download a controller-generated portable project archive for sharing."""
    user = current_user(request)
    require_project_access(request, project_id)
    try:
        project = await gns3("GET", f"/v2/projects/{project_id}")
    except HTTPException as exc:
        return error_page(request, exc)
    safe_name = "".join(char if char.isalnum() or char in "-_. " else "_" for char in str(project.get("name", "lab"))).strip() or "lab"
    audit(audit_actor(request), "export_lab", project_id, {"name": project.get("name")})
    return StreamingResponse(
        stream_project_export(project_id),
        media_type="application/gns3project",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}.gns3project"'},
    )


@app.get("/projects/{project_id}/snapshots/{snapshot_id}/download")
async def download_snapshot(request: Request, project_id: str, snapshot_id: str):
    """Download one native snapshot for controlled sharing or off-server storage."""
    user = current_user(request)
    require_project_access(request, project_id)
    try:
        project = await gns3("GET", f"/v2/projects/{project_id}")
        snapshots = await gns3("GET", f"/v2/projects/{project_id}/snapshots")
    except HTTPException as exc:
        return error_page(request, exc)
    snapshot = next((item for item in snapshots if str(item.get("snapshot_id")) == snapshot_id), None)
    snapshot_path = local_snapshot_file(project, snapshot or {})
    if not snapshot or not snapshot_path:
        return error_page(request, HTTPException(409, "This backup cannot be downloaded. Configure GNS3_PROJECTS_DIR and grant the portal read access to the snapshots directory."))
    audit(audit_actor(request), "download_backup", project_id, {"snapshot_id": snapshot_id, "snapshot": snapshot.get("name")})
    return FileResponse(snapshot_path, media_type="application/gns3project", filename=snapshot_path.name)


async def queue_snapshot_import(request: Request, project_id: str, snapshot_id: str, name: str) -> RedirectResponse:
    """Queue a distinct stopped lab from a source project's native snapshot."""
    user = current_user(request)
    require_project_access(request, project_id)
    new_name = name.strip()
    if not new_name or len(new_name) > 80:
        return RedirectResponse("/?error=Use+a+lab+name+between+1+and+80+characters", status_code=303)
    if not GNS3_PROJECTS_DIR:
        return RedirectResponse("/?error=Configure+GNS3_PROJECTS_DIR+before+creating+a+lab+from+a+backup", status_code=303)
    active_states = {"preparing", "importing"}
    if any(job.get("status") in active_states for job in app.state.import_jobs.values()):
        return RedirectResponse("/?error=Another+backup+import+is+already+running", status_code=303)
    job_id = str(uuid.uuid4())
    job = {
        "source_project_id": project_id,
        "snapshot_id": snapshot_id,
        "new_project_id": str(uuid.uuid4()),
        "new_name": new_name,
        "actor": user,
        "audit_actor": audit_actor(request),
        "status": "preparing",
        "progress_percent": 0,
        "message": "Import queued…",
        "project_id": None,
    }
    app.state.import_jobs[job_id] = job
    asyncio.create_task(run_snapshot_import_job(job))
    return RedirectResponse(f"/?import_job={job_id}", status_code=303)


@app.post("/backup-imports")
async def create_lab_from_backup(request: Request, source_project_id: str = Form(...), snapshot_id: str = Form(...), name: str = Form(...)):
    require_permission(request, "labs.create")
    """Create a new lab from a selected backup on the dashboard."""
    return await queue_snapshot_import(request, source_project_id, snapshot_id, name)


@app.post("/shared-backup-imports")
async def create_lab_from_shared_backup(request: Request, source_project_id: str = Form(...), snapshot_id: str = Form(...), name: str = Form(...)):
    require_permission(request, "labs.create")
    user = current_user(request)
    if not has_admin_scope(request) and not any(item["project_id"] == source_project_id and item["snapshot_id"] == snapshot_id for item in backup_shares_for(user)):
        raise HTTPException(404, "Shared backup not found.")
    new_name = name.strip()
    if not new_name or len(new_name) > 80:
        return RedirectResponse("/?error=Use+a+lab+name+between+1+and+80+characters", status_code=303)
    if not GNS3_PROJECTS_DIR:
        return RedirectResponse("/?error=Configure+GNS3_PROJECTS_DIR+before+creating+a+lab+from+a+backup", status_code=303)
    if any(job.get("status") in {"preparing", "importing"} for job in app.state.import_jobs.values()):
        return RedirectResponse("/?error=Another+backup+import+is+already+running", status_code=303)
    job_id = str(uuid.uuid4())
    app.state.import_jobs[job_id] = {"source_project_id":source_project_id, "snapshot_id":snapshot_id, "new_project_id":str(uuid.uuid4()), "new_name":new_name, "actor":user, "audit_actor":audit_actor(request), "consume_backup_share":True, "status":"preparing", "progress_percent":0, "message":"Shared backup queued for import…", "project_id":None}
    asyncio.create_task(run_snapshot_import_job(app.state.import_jobs[job_id]))
    audit(audit_actor(request), "create_lab_from_shared_backup", source_project_id, {"snapshot_id": snapshot_id, "new_name": new_name})
    return RedirectResponse(f"/?import_job={job_id}", status_code=303)


@app.post("/template-imports")
async def create_lab_from_template(request: Request, template_id: str = Form(...), name: str = Form(...)):
    require_permission(request, "labs.create")
    user = current_user(request)
    template = template_record(template_id)
    if not template or template.get("approval_status") != "approved":
        raise HTTPException(404, "Lab template not found.")
    archive_path = Path(str(template.get("archive_path") or ""))
    try:
        archive_path.resolve(strict=True).relative_to(LAB_TEMPLATE_DIR.resolve(strict=True))
    except (OSError, ValueError):
        return RedirectResponse("/?error=Template+archive+is+not+available", status_code=303)
    new_name = name.strip()
    if not new_name or len(new_name) > 80:
        return RedirectResponse("/?error=Use+a+lab+name+between+1+and+80+characters", status_code=303)
    if any(job.get("status") in {"preparing", "importing"} for job in app.state.import_jobs.values()):
        return RedirectResponse("/?error=Another+backup+import+is+already+running", status_code=303)
    job_id = str(uuid.uuid4())
    app.state.import_jobs[job_id] = {"source_project_id":"", "snapshot_id":"", "template_path":str(archive_path), "template_id":template_id, "new_project_id":str(uuid.uuid4()), "new_name":new_name, "actor":user, "audit_actor":audit_actor(request), "status":"preparing", "progress_percent":0, "message":"Lab template queued for import…", "project_id":None}
    asyncio.create_task(run_snapshot_import_job(app.state.import_jobs[job_id]))
    audit(audit_actor(request), "create_lab_from_template", template_id, {"new_name": new_name})
    return RedirectResponse(f"/?import_job={job_id}", status_code=303)


@app.post("/retained-backup-imports")
async def create_lab_from_retained_backup(request: Request, backup_id: str = Form(...), name: str = Form(...)):
    require_permission(request, "labs.create")
    user = current_user(request)
    record = next((item for item in retained_backups_for(None if is_admin(request) else user) if item["backup_id"] == backup_id), None)
    if not record or not Path(record["archive_path"]).is_file():
        return RedirectResponse("/?error=Retained+backup+not+found", status_code=303)
    if not name.strip() or len(name.strip()) > 80:
        return RedirectResponse("/?error=Use+a+lab+name+between+1+and+80+characters", status_code=303)
    job_id = str(uuid.uuid4())
    app.state.import_jobs[job_id] = {"source_project_id":"", "snapshot_id":"", "retained_path":record["archive_path"], "new_project_id":str(uuid.uuid4()), "new_name":name.strip(), "actor":user, "audit_actor":audit_actor(request), "status":"preparing", "progress_percent":0, "message":"Retained backup queued for import…", "project_id":None}
    asyncio.create_task(run_snapshot_import_job(app.state.import_jobs[job_id]))
    return RedirectResponse(f"/?import_job={job_id}", status_code=303)


@app.post("/backup-uploads")
async def upload_backup_as_lab(request: Request, name: str = Form(...), backup_file: UploadFile = File(...)):
    require_permission(request, "labs.create")
    """Accept a portable/native backup and queue a new powered-off lab import."""
    user = current_user(request)
    new_name = name.strip()
    suffix = Path(backup_file.filename or "").suffix.lower()
    if not new_name or len(new_name) > 80:
        return RedirectResponse("/?error=Use+a+lab+name+between+1+and+80+characters", status_code=303)
    if suffix not in {".gns3snapshot", ".gns3project"}:
        return RedirectResponse("/?error=Upload+a+.gns3snapshot+or+.gns3project+backup+file", status_code=303)
    if any(job.get("status") in {"preparing", "importing"} for job in app.state.import_jobs.values()):
        return RedirectResponse("/?error=Another+backup+import+is+already+running", status_code=303)
    upload_path = UPLOAD_DIR / f"{uuid.uuid4()}{suffix}"
    try:
        with upload_path.open("wb") as destination:
            while chunk := await backup_file.read(1024 * 1024):
                destination.write(chunk)
    except OSError:
        upload_path.unlink(missing_ok=True)
        return RedirectResponse("/?error=The+portal+could+not+store+the+uploaded+backup", status_code=303)
    finally:
        await backup_file.close()
    job_id = str(uuid.uuid4())
    job = {"source_project_id": "", "snapshot_id": "", "upload_path": str(upload_path), "new_project_id": str(uuid.uuid4()), "new_name": new_name, "actor": user, "audit_actor": audit_actor(request), "status": "preparing", "progress_percent": 0, "message": "Uploaded backup queued for import…", "project_id": None}
    app.state.import_jobs[job_id] = job
    asyncio.create_task(run_snapshot_import_job(job))
    audit(audit_actor(request), "upload_backup", "portal", {"filename": Path(backup_file.filename or "").name, "new_name": new_name})
    return RedirectResponse(f"/?import_job={job_id}", status_code=303)


@app.post("/projects/{project_id}/snapshots/{snapshot_id}/clone")
async def clone_snapshot_as_lab(request: Request, project_id: str, snapshot_id: str, name: str = Form(...)):
    require_permission(request, "labs.create")
    """Legacy endpoint retained for existing links; it returns to the dashboard."""
    require_project_access(request, project_id)
    return await queue_snapshot_import(request, project_id, snapshot_id, name)


@app.get("/projects/{project_id}/import-jobs/{job_id}")
async def import_job_status(request: Request, project_id: str, job_id: str):
    current_user(request)
    require_project_access(request, project_id)
    job = app.state.import_jobs.get(job_id)
    if not job or job.get("source_project_id") != project_id:
        raise HTTPException(404, "Import job not found. It may have been cleared after a portal restart.")
    return JSONResponse({key: job.get(key) for key in ("status", "progress_percent", "message", "project_id")})


@app.get("/backup-import-jobs/{job_id}")
async def dashboard_import_job_status(request: Request, job_id: str):
    """Return a dashboard import job without requiring a source-lab URL."""
    current_user(request)
    job = app.state.import_jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Import job not found. It may have been cleared after a portal restart.")
    return JSONResponse({key: job.get(key) for key in ("status", "progress_percent", "message", "project_id")})


@app.post("/projects/{project_id}/rename")
async def rename_project(request: Request, project_id: str, name: str = Form(...), return_to: str = Form("")):
    require_permission(request, "labs.edit")
    user = current_user(request)
    require_project_access(request, project_id)
    new_name = name.strip()
    if not new_name or len(new_name) > 80:
        return redirect_back(return_to, project_id, "error=Use+a+lab+name+between+1+and+80+characters")
    try:
        project = await gns3("GET", f"/v2/projects/{project_id}")
        old_name = str(project.get("name", ""))
        if new_name == old_name:
            return redirect_back(return_to, project_id)
        await gns3("PUT", f"/v2/projects/{project_id}", json={"name": new_name})
    except HTTPException as exc:
        return redirect_back(return_to, project_id, f"error={quote_plus(str(exc.detail))}")
    audit(audit_actor(request), "rename_lab", project_id, {"old_name": old_name, "new_name": new_name})
    return redirect_back(return_to, project_id, "message=Lab+renamed")


@app.post("/projects/{project_id}/snapshots/{snapshot_id}/restore")
async def restore_snapshot(request: Request, project_id: str, snapshot_id: str):
    require_permission(request, "labs.edit")
    require_project_access(request, project_id)
    require_lab_owner(request, project_id)
    user = current_user(request)
    require_project_access(request, project_id)
    try:
        await ensure_project_open(project_id)
        await gns3("POST", f"/v2/projects/{project_id}/snapshots/{snapshot_id}/restore", json={})
    except HTTPException as exc:
        return RedirectResponse(f"/projects/{project_id}?error={quote_plus(str(exc.detail))}", status_code=303)
    audit(audit_actor(request), "restore_snapshot", project_id, {"snapshot_id": snapshot_id})
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@app.post("/projects/{project_id}/snapshots/{snapshot_id}/delete")
async def delete_snapshot(request: Request, project_id: str, snapshot_id: str):
    require_permission(request, "labs.edit")
    require_project_access(request, project_id)
    require_lab_owner(request, project_id)
    user = current_user(request)
    require_project_access(request, project_id)
    try:
        await ensure_project_open(project_id)
        await gns3("DELETE", f"/v2/projects/{project_id}/snapshots/{snapshot_id}")
    except HTTPException as exc:
        return RedirectResponse(f"/projects/{project_id}?error={quote_plus(str(exc.detail))}", status_code=303)
    with db.connect() as connection:
        connection.execute("DELETE FROM backup_shares WHERE project_id=? AND snapshot_id=?", (project_id, snapshot_id))
    audit(audit_actor(request), "delete_snapshot", project_id, {"snapshot_id": snapshot_id})
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@app.post("/projects/{project_id}/snapshots/delete")
async def delete_selected_snapshots(request: Request, project_id: str, snapshot_ids: list[str] = Form(...)):
    require_permission(request, "labs.edit")
    require_project_access(request, project_id)
    require_lab_owner(request, project_id)
    """Permanently remove selected completed backups after browser confirmation."""
    require_project_access(request, project_id)
    selected_ids = {snapshot_id for snapshot_id in snapshot_ids if snapshot_id}
    if not selected_ids:
        return RedirectResponse(f"/projects/{project_id}?error=Select+at+least+one+backup", status_code=303)
    snapshots = await gns3("GET", f"/v2/projects/{project_id}/snapshots")
    selected = [item for item in snapshots if str(item.get("snapshot_id")) in selected_ids]
    if len(selected) != len(selected_ids):
        return RedirectResponse(f"/projects/{project_id}?error=One+or+more+selected+backups+were+not+found", status_code=303)
    await ensure_project_open(project_id)
    deleted: list[str] = []
    failed = 0
    for snapshot in selected:
        snapshot_id = str(snapshot["snapshot_id"])
        try:
            await gns3("DELETE", f"/v2/projects/{project_id}/snapshots/{snapshot_id}")
        except HTTPException:
            failed += 1
            portal_logger.warning("bulk_snapshot_delete_failed project_id=%s snapshot_id=%s", project_id, snapshot_id)
            continue
        with db.connect() as connection:
            connection.execute("DELETE FROM backup_shares WHERE project_id=? AND snapshot_id=?", (project_id, snapshot_id))
        deleted.append(snapshot_id)
    if deleted:
        audit(audit_actor(request), "delete_selected_snapshots", project_id, {"snapshot_ids": deleted, "failed": failed})
    message = f"Deleted+{len(deleted)}+backup(s)"
    if failed:
        message += f";+{failed}+could+not+be+deleted"
    return RedirectResponse(f"/projects/{project_id}?message={message}", status_code=303)


@app.post("/projects/{project_id}/delete")
async def delete_project(request: Request, project_id: str, confirmation: str = Form(...), retained_snapshot_ids: list[str] = Form([])):
    require_permission(request, "labs.edit")
    require_project_access(request, project_id)
    require_lab_owner(request, project_id)
    user = current_user(request)
    require_project_access(request, project_id)
    if not can_manage_lab_sharing(request, project_id):
        raise HTTPException(403, "Only the lab owner or an Administrator can delete this lab.")
    project = await gns3("GET", f"/v2/projects/{project_id}")
    if confirmation.strip() != project.get("name"):
        return RedirectResponse(f"/projects/{project_id}?error=Lab+name+did+not+match", status_code=303)
    retained = 0
    if retained_snapshot_ids:
        if not GNS3_PROJECTS_DIR:
            return RedirectResponse(f"/projects/{project_id}?error=Configure+GNS3_PROJECTS_DIR+before+keeping+backups", status_code=303)
        snapshots = await gns3("GET", f"/v2/projects/{project_id}/snapshots")
        selected = {snapshot_id for snapshot_id in retained_snapshot_ids if snapshot_id}
        selected_snapshots = [snapshot for snapshot in snapshots if str(snapshot.get("snapshot_id")) in selected]
        if len(selected_snapshots) != len(selected):
            return RedirectResponse(f"/projects/{project_id}?error=One+or+more+selected+backups+were+not+found", status_code=303)
        available = retained_backup_limit(user) - retained_backup_count(user)
        if len(selected_snapshots) > available:
            return RedirectResponse(f"/projects/{project_id}?error={quote_plus(f'You can retain only {max(available, 0)} more backup(s). Your limit is {retained_backup_limit(user)}.')}", status_code=303)
        target_dir = RETAINED_BACKUP_DIR / project_id
        target_dir.mkdir(parents=True, exist_ok=True)
        for snapshot in selected_snapshots:
            source = local_snapshot_file(project, snapshot)
            if not source:
                return RedirectResponse(f"/projects/{project_id}?error=One+or+more+native+backup+files+could+not+be+found", status_code=303)
            backup_id = str(uuid.uuid4())
            destination = target_dir / f"{backup_id}.gns3snapshot"
            await asyncio.to_thread(shutil.copy2, source, destination)
            with db.connect() as connection:
                connection.execute("INSERT INTO retained_backups(backup_id,owner,source_project_id,source_lab_name,backup_name,archive_path,size_bytes,retained_at) VALUES(?,?,?,?,?,?,?,?)", (backup_id, user, project_id, str(project.get("name", "Lab")), str(snapshot.get("name", "Unnamed backup")), str(destination), destination.stat().st_size, datetime.now(timezone.utc).isoformat()))
            retained += 1
    await gns3("DELETE", f"/v2/projects/{project_id}")
    with db.connect() as connection:
        connection.execute("DELETE FROM lab_owners WHERE project_id=?", (project_id,))
        connection.execute("DELETE FROM lab_shares WHERE project_id=?", (project_id,))
        connection.execute("DELETE FROM backup_shares WHERE project_id=?", (project_id,))
    audit(audit_actor(request), "delete_lab", project_id, {"name": project.get("name"), "retained_backups": retained})
    return RedirectResponse(f"/?message={quote_plus(f'Lab deleted. {retained} backup(s) retained.')}", status_code=303)
