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
import sqlite3
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
from starlette.middleware.sessions import SessionMiddleware

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
AUTH_ENABLED = os.getenv("PORTAL_AUTH_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
SESSION_IDLE_SECONDS = max(60, int(os.getenv("PORTAL_SESSION_IDLE_SECONDS", "1800")))
SESSION_MAX_SECONDS = max(SESSION_IDLE_SECONDS, int(os.getenv("PORTAL_SESSION_MAX_SECONDS", "28800")))
AUDIT_DB = Path(os.getenv("AUDIT_DB", str(BASE_DIR / "data" / "audit.db")))
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
    AUDIT_DB.parent.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    USER_AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    RETAINED_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    LAB_TEMPLATE_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(AUDIT_DB) as connection:
        connection.execute("""CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL, actor TEXT NOT NULL,
            action TEXT NOT NULL, target TEXT NOT NULL, details TEXT
        )""")
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
        connection.execute("DELETE FROM portal_sessions WHERE expires_at < ?", (datetime.now(timezone.utc).isoformat(),))
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
        connection.execute("""CREATE TABLE IF NOT EXISTS lab_template_access (
            template_id TEXT NOT NULL, subject_type TEXT NOT NULL CHECK(subject_type IN ('local_user','directory_group')),
            subject_id TEXT NOT NULL COLLATE NOCASE, granted_by TEXT NOT NULL COLLATE NOCASE,
            granted_at TEXT NOT NULL, PRIMARY KEY(template_id, subject_type, subject_id)
        )""")
        # Preserve the prior deployment path: the existing environment login
        # becomes the first local administrator on the first RBAC startup.
        existing = connection.execute("SELECT 1 FROM portal_users LIMIT 1").fetchone()
        if not existing:
            connection.execute(
                "INSERT INTO portal_users(username,password_hash,role,enabled,created_at) VALUES(?,?,?,?,?)",
                (PORTAL_USERNAME, password_hash(PORTAL_PASSWORD), "admin", 1, datetime.now(timezone.utc).isoformat()),
            )


def audit(actor: str, action: str, target: str, details: dict[str, Any] | None = None) -> None:
    with sqlite3.connect(AUDIT_DB) as connection:
        connection.execute(
            "INSERT INTO audit_log(timestamp, actor, action, target, details) VALUES (?, ?, ?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), actor, action, target, json.dumps(details or {})),
        )


def recent_audit(limit: int = 20, username: str | None = None, include_all: bool = False) -> list[dict[str, str]]:
    with sqlite3.connect(AUDIT_DB) as connection:
        connection.row_factory = sqlite3.Row
        if include_all or not username:
            rows = connection.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        else:
            rows = connection.execute("SELECT * FROM audit_log WHERE actor=? OR actor LIKE ? ORDER BY id DESC LIMIT ?", (username, f"% impersonating {username}", limit)).fetchall()
    return [dict(row) for row in rows]


def password_hash(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def password_matches(password: str, stored: str) -> bool:
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
    with sqlite3.connect(AUDIT_DB) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute("SELECT username,password_hash,role,enabled,created_at,email,first_name,last_name,avatar_filename,retained_backup_limit FROM portal_users WHERE username=?", (username,)).fetchone()
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
    with sqlite3.connect(AUDIT_DB) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute("SELECT username,role,enabled,created_at,email,first_name,last_name,avatar_filename,retained_backup_limit FROM portal_users ORDER BY username COLLATE NOCASE").fetchall()
    return [dict(row) for row in rows]


def enabled_admin_count() -> int:
    with sqlite3.connect(AUDIT_DB) as connection:
        row = connection.execute("SELECT COUNT(*) FROM portal_users WHERE role='admin' AND enabled=1").fetchone()
    return int(row[0])


def lab_owner(project_id: str) -> str | None:
    with sqlite3.connect(AUDIT_DB) as connection:
        row = connection.execute("SELECT username FROM lab_owners WHERE project_id=?", (project_id,)).fetchone()
    return str(row[0]) if row else None


def set_lab_owner(project_id: str, username: str) -> None:
    with sqlite3.connect(AUDIT_DB) as connection:
        connection.execute("INSERT INTO lab_owners(project_id,username,assigned_at) VALUES(?,?,?) ON CONFLICT(project_id) DO UPDATE SET username=excluded.username, assigned_at=excluded.assigned_at", (project_id, username, datetime.now(timezone.utc).isoformat()))


def owned_project_ids(username: str) -> set[str]:
    with sqlite3.connect(AUDIT_DB) as connection:
        rows = connection.execute("SELECT project_id FROM lab_owners WHERE username=?", (username,)).fetchall()
    return {str(row[0]) for row in rows}


def shared_project_ids(username: str) -> set[str]:
    with sqlite3.connect(AUDIT_DB) as connection:
        rows = connection.execute("SELECT project_id FROM lab_shares WHERE username=?", (username,)).fetchall()
    return {str(row[0]) for row in rows}


def shared_users(project_id: str) -> list[str]:
    with sqlite3.connect(AUDIT_DB) as connection:
        rows = connection.execute("SELECT username FROM lab_shares WHERE project_id=? ORDER BY username COLLATE NOCASE", (project_id,)).fetchall()
    return [str(row[0]) for row in rows]


def backup_shared_users(project_id: str, snapshot_id: str) -> list[str]:
    with sqlite3.connect(AUDIT_DB) as connection:
        rows = connection.execute("SELECT username FROM backup_shares WHERE project_id=? AND snapshot_id=? ORDER BY username COLLATE NOCASE", (project_id, snapshot_id)).fetchall()
    return [str(row[0]) for row in rows]


def backup_shares_for(username: str) -> list[dict[str, str]]:
    with sqlite3.connect(AUDIT_DB) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute("SELECT project_id,snapshot_id,shared_by,shared_at FROM backup_shares WHERE username=? ORDER BY shared_at DESC", (username,)).fetchall()
    return [dict(row) for row in rows]


def retained_backups_for(username: str | None = None) -> list[dict[str, Any]]:
    query, values = "SELECT * FROM retained_backups", ()
    if username:
        query += " WHERE owner=?"; values = (username,)
    query += " ORDER BY retained_at DESC"
    with sqlite3.connect(AUDIT_DB) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(query, values).fetchall()
    return [dict(row) for row in rows]


def retained_backup_limit(username: str) -> int:
    account = user_record(username)
    return max(0, int(account.get("retained_backup_limit") or 2)) if account else 2


def retained_backup_count(username: str) -> int:
    with sqlite3.connect(AUDIT_DB) as connection:
        row = connection.execute("SELECT COUNT(*) FROM retained_backups WHERE owner=?", (username,)).fetchone()
    return int(row[0])


def templates_for_user(username: str, include_owned: bool = False) -> list[dict[str, Any]]:
    with sqlite3.connect(AUDIT_DB) as connection:
        connection.row_factory = sqlite3.Row
        if include_owned:
            rows = connection.execute("SELECT * FROM lab_templates ORDER BY created_at DESC").fetchall()
        else:
            rows = connection.execute("SELECT DISTINCT t.* FROM lab_templates t LEFT JOIN lab_template_access a ON a.template_id=t.template_id WHERE t.owner=? OR (t.approval_status='approved' AND a.subject_type='local_user' AND a.subject_id=?) ORDER BY t.created_at DESC", (username, username)).fetchall()
    return [dict(row) for row in rows]


def approved_templates() -> list[dict[str, Any]]:
    with sqlite3.connect(AUDIT_DB) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute("SELECT * FROM lab_templates WHERE approval_status='approved' ORDER BY created_at DESC").fetchall()
    return [dict(row) for row in rows]


def template_access_users(template_id: str) -> list[str]:
    with sqlite3.connect(AUDIT_DB) as connection:
        rows = connection.execute("SELECT subject_id FROM lab_template_access WHERE template_id=? AND subject_type='local_user' ORDER BY subject_id COLLATE NOCASE", (template_id,)).fetchall()
    return [str(row[0]) for row in rows]


def template_record(template_id: str) -> dict[str, Any] | None:
    with sqlite3.connect(AUDIT_DB) as connection:
        connection.row_factory = sqlite3.Row
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
    auth = (GNS3_USERNAME, GNS3_PASSWORD) if GNS3_USERNAME else None
    timeout = httpx.Timeout(GNS3_TIMEOUT_SECONDS, connect=10)
    limits = httpx.Limits(max_connections=40, max_keepalive_connections=12, keepalive_expiry=30)
    app_instance.state.gns3_client = httpx.AsyncClient(timeout=timeout, auth=auth, limits=limits)
    app_instance.state.backup_jobs = {}
    app_instance.state.import_jobs = {}
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
templates = Jinja2Templates(directory=BASE_DIR / "templates")
templates.env.globals["auth_enabled"] = AUTH_ENABLED
templates.env.filters["storage_size"] = format_storage_size


def session_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def revoke_portal_session(request: Request) -> None:
    token = str(request.session.get("sid") or "")
    if token:
        with sqlite3.connect(AUDIT_DB) as connection:
            connection.execute("DELETE FROM portal_sessions WHERE session_id_hash=?", (session_token_hash(token),))
    request.session.clear()


def active_portal_session(request: Request) -> dict[str, Any] | None:
    token = str(request.session.get("sid") or "")
    if not token:
        return None
    now = datetime.now(timezone.utc)
    with sqlite3.connect(AUDIT_DB) as connection:
        connection.row_factory = sqlite3.Row
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
    with sqlite3.connect(AUDIT_DB) as connection:
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
    return str(account["username"])


def is_admin(request: Request) -> bool:
    if not AUTH_ENABLED:
        return True
    record = user_record(signed_in_user(request))
    return bool(record and record.get("role") == "admin")


def can_manage_users(request: Request) -> bool:
    """UI permission: only a signed-in Admin outside impersonation mode."""
    return bool(AUTH_ENABLED and not request.session.get("impersonating") and is_admin(request))


def has_admin_scope(request: Request) -> bool:
    """Actual Admin permissions, unless deliberately operating as another user."""
    return bool(is_admin(request) and not request.session.get("impersonating"))


def can_manage_lab_sharing(request: Request, project_id: str) -> bool:
    return bool(has_admin_scope(request) or lab_owner(project_id) == current_user(request))


def audit_actor(request: Request) -> str:
    signed_in = signed_in_user(request)
    effective = current_user(request)
    return signed_in if signed_in == effective else f"{signed_in} impersonating {effective}"


def require_admin(request: Request) -> None:
    if not is_admin(request):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Administrator access is required.")


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
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
    except ValueError:
        return str(value)


templates.env.filters["backup_datetime"] = format_backup_datetime
templates.env.filters["account_display_name"] = account_display_name
templates.env.filters["account_has_avatar"] = account_has_avatar


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
    auth = (GNS3_USERNAME, GNS3_PASSWORD) if GNS3_USERNAME else None
    timeout = httpx.Timeout(GNS3_TIMEOUT_SECONDS, connect=10)
    async with httpx.AsyncClient(timeout=timeout, auth=auth) as client:
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

    auth = (GNS3_USERNAME, GNS3_PASSWORD) if GNS3_USERNAME else None
    timeout = httpx.Timeout(GNS3_TIMEOUT_SECONDS, connect=10)
    params = {
        "name": str(job["new_name"]),
        "auto_start": "false",
        "auto_open": "false",
        "auto_close": "true",
    }
    try:
        async with httpx.AsyncClient(timeout=timeout, auth=auth) as client:
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
            with sqlite3.connect(AUDIT_DB) as connection:
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
    return templates.TemplateResponse(request, "error.html", {"message": str(exc.detail), "status": exc.status_code}, status_code=exc.status_code)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if not AUTH_ENABLED:
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(request, "login.html", {"error": None, "login_page": True})


@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    if not AUTH_ENABLED:
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    account = user_record(username.strip())
    if not account or not account.get("enabled") or not password_matches(password, str(account.get("password_hash", ""))):
        return templates.TemplateResponse(request, "login.html", {"error": "Invalid username or password.", "login_page": True}, status_code=401)
    create_portal_session(request, str(account["username"]))
    audit(str(account["username"]), "login", "portal", {"role": account["role"]})
    return RedirectResponse("/", status_code=303)


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
    with sqlite3.connect(AUDIT_DB) as connection:
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
    require_admin(request)
    return templates.TemplateResponse(request, "admin_users.html", {
        "users": list_users(), "user": current_user(request), "signed_in_user": signed_in_user(request),
        "is_admin": True, "can_manage_users": True, "impersonating": request.session.get("impersonating", ""),
    })


@app.post("/admin/users")
async def admin_create_user(request: Request, username: str = Form(...), password: str = Form(...), role: str = Form("user")):
    require_admin(request)
    username, role = username.strip(), role.strip().lower()
    if not username or len(username) > 80 or any(char.isspace() for char in username) or role not in {"admin", "user"} or len(password) < 12:
        return RedirectResponse("/admin/users?error=Use+a+username+without+spaces%2C+a+valid+role%2C+and+a+password+of+at+least+12+characters", status_code=303)
    try:
        with sqlite3.connect(AUDIT_DB) as connection:
            connection.execute("INSERT INTO portal_users(username,password_hash,role,enabled,created_at) VALUES(?,?,?,?,?)", (username, password_hash(password), role, 1, datetime.now(timezone.utc).isoformat()))
    except sqlite3.IntegrityError:
        return RedirectResponse("/admin/users?error=That+username+already+exists", status_code=303)
    audit(audit_actor(request), "create_user", username, {"role": role})
    return RedirectResponse("/admin/users?message=User+created", status_code=303)


@app.post("/admin/users/{username}/update")
async def admin_update_user(request: Request, username: str, role: str = Form(...), enabled: str = Form("0"), password: str = Form(""), retained_backup_limit: int = Form(2)):
    require_admin(request)
    target = user_record(username)
    role = role.strip().lower()
    if not target or role not in {"admin", "user"}:
        raise HTTPException(404, "User not found.")
    if retained_backup_limit < 0 or retained_backup_limit > 100:
        return RedirectResponse("/admin/users?error=Retained+backup+limit+must+be+between+0+and+100", status_code=303)
    if target["username"].lower() == signed_in_user(request).lower() and enabled != "1":
        return RedirectResponse("/admin/users?error=You+cannot+disable+your+own+account", status_code=303)
    if target.get("role") == "admin" and target.get("enabled") and (role != "admin" or enabled != "1") and enabled_admin_count() <= 1:
        return RedirectResponse("/admin/users?error=At+least+one+enabled+Administrator+must+remain", status_code=303)
    with sqlite3.connect(AUDIT_DB) as connection:
        if password:
            if len(password) < 12:
                return RedirectResponse("/admin/users?error=Passwords+must+be+at+least+12+characters", status_code=303)
            connection.execute("UPDATE portal_users SET role=?,enabled=?,password_hash=?,retained_backup_limit=? WHERE username=?", (role, 1 if enabled == "1" else 0, password_hash(password), retained_backup_limit, target["username"]))
        else:
            connection.execute("UPDATE portal_users SET role=?,enabled=?,retained_backup_limit=? WHERE username=?", (role, 1 if enabled == "1" else 0, retained_backup_limit, target["username"]))
    audit(audit_actor(request), "update_user", target["username"], {"role": role, "enabled": enabled == "1", "password_reset": bool(password), "retained_backup_limit": retained_backup_limit})
    return RedirectResponse("/admin/users?message=User+updated", status_code=303)


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
    require_admin(request)
    target = user_record(username)
    if not target or not target.get("enabled"):
        return RedirectResponse("/?error=Choose+an+enabled+portal+user", status_code=303)
    set_lab_owner(project_id, target["username"])
    audit(audit_actor(request), "assign_lab_owner", project_id, {"owner": target["username"]})
    return RedirectResponse("/?message=Lab+owner+updated", status_code=303)


@app.post("/projects/{project_id}/shares")
async def share_lab(request: Request, project_id: str, username: str = Form(...)):
    require_project_access(request, project_id)
    if not can_manage_lab_sharing(request, project_id):
        raise HTTPException(403, "Only the lab owner or an Administrator can share this lab.")
    target = user_record(username)
    owner = lab_owner(project_id)
    if not target or not target.get("enabled") or target["username"] == owner:
        return RedirectResponse("/?error=Choose+another+enabled+portal+user", status_code=303)
    with sqlite3.connect(AUDIT_DB) as connection:
        connection.execute("INSERT INTO lab_shares(project_id,username,shared_by,shared_at) VALUES(?,?,?,?) ON CONFLICT(project_id,username) DO UPDATE SET shared_by=excluded.shared_by, shared_at=excluded.shared_at", (project_id, target["username"], audit_actor(request), datetime.now(timezone.utc).isoformat()))
    audit(audit_actor(request), "share_lab", project_id, {"shared_with": target["username"]})
    return RedirectResponse("/?message=Lab+shared", status_code=303)


@app.post("/projects/{project_id}/shares/{username}/remove")
async def remove_lab_share(request: Request, project_id: str, username: str):
    require_project_access(request, project_id)
    if not can_manage_lab_sharing(request, project_id):
        raise HTTPException(403, "Only the lab owner or an Administrator can change sharing.")
    with sqlite3.connect(AUDIT_DB) as connection:
        connection.execute("DELETE FROM lab_shares WHERE project_id=? AND username=?", (project_id, username))
    audit(audit_actor(request), "remove_lab_share", project_id, {"user": username})
    return RedirectResponse("/?message=Lab+sharing+updated", status_code=303)


@app.post("/projects/{project_id}/snapshots/{snapshot_id}/shares")
async def share_backup(request: Request, project_id: str, snapshot_id: str, username: str = Form(...)):
    require_project_access(request, project_id)
    if not can_manage_lab_sharing(request, project_id):
        raise HTTPException(403, "Only the lab owner or an Administrator can share a backup.")
    target = user_record(username)
    if not target or not target.get("enabled") or target["username"] == lab_owner(project_id):
        return RedirectResponse(f"/projects/{project_id}?error=Choose+another+enabled+portal+user", status_code=303)
    snapshots = await gns3("GET", f"/v2/projects/{project_id}/snapshots")
    if not any(str(item.get("snapshot_id")) == snapshot_id for item in snapshots):
        raise HTTPException(404, "Backup not found.")
    with sqlite3.connect(AUDIT_DB) as connection:
        connection.execute("INSERT INTO backup_shares(project_id,snapshot_id,username,shared_by,shared_at) VALUES(?,?,?,?,?) ON CONFLICT(project_id,snapshot_id,username) DO UPDATE SET shared_by=excluded.shared_by, shared_at=excluded.shared_at", (project_id, snapshot_id, target["username"], audit_actor(request), datetime.now(timezone.utc).isoformat()))
    audit(audit_actor(request), "share_backup", project_id, {"snapshot_id": snapshot_id, "shared_with": target["username"]})
    return RedirectResponse(f"/projects/{project_id}?message=Backup+shared", status_code=303)


@app.post("/projects/{project_id}/snapshots/{snapshot_id}/shares/{username}/remove")
async def remove_backup_share(request: Request, project_id: str, snapshot_id: str, username: str):
    require_project_access(request, project_id)
    if not can_manage_lab_sharing(request, project_id):
        raise HTTPException(403, "Only the lab owner or an Administrator can change backup sharing.")
    with sqlite3.connect(AUDIT_DB) as connection:
        connection.execute("DELETE FROM backup_shares WHERE project_id=? AND snapshot_id=? AND username=?", (project_id, snapshot_id, username))
    audit(audit_actor(request), "remove_backup_share", project_id, {"snapshot_id": snapshot_id, "user": username})
    return RedirectResponse(f"/projects/{project_id}?message=Backup+sharing+updated", status_code=303)


@app.post("/projects/{project_id}/snapshots/{snapshot_id}/template")
async def create_lab_template(request: Request, project_id: str, snapshot_id: str, name: str = Form(...), description: str = Form("")):
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
        with sqlite3.connect(AUDIT_DB) as connection:
            status_value = "approved" if has_admin_scope(request) else "pending"
            approved_by = audit_actor(request) if status_value == "approved" else None
            approved_at = datetime.now(timezone.utc).isoformat() if status_value == "approved" else None
            connection.execute("INSERT INTO lab_templates(template_id,owner,source_project_id,snapshot_id,name,description,archive_path,size_bytes,created_at,approval_status,approved_by,approved_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (template_id, current_user(request), project_id, snapshot_id, name, description or None, str(destination), destination.stat().st_size, datetime.now(timezone.utc).isoformat(), status_value, approved_by, approved_at))
    except sqlite3.IntegrityError:
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
    if not template or (not has_admin_scope(request) and template["owner"] != current_user(request)):
        raise HTTPException(404, "Lab template not found.")
    target = user_record(username.strip())
    if not target or not target.get("enabled"):
        return RedirectResponse(f"/projects/{template['source_project_id']}?error=Choose+an+enabled+portal+user", status_code=303)
    with sqlite3.connect(AUDIT_DB) as connection:
        connection.execute("INSERT INTO lab_template_access(template_id,subject_type,subject_id,granted_by,granted_at) VALUES(?,?,?,?,?) ON CONFLICT(template_id,subject_type,subject_id) DO UPDATE SET granted_by=excluded.granted_by,granted_at=excluded.granted_at", (template_id, "local_user", target["username"], audit_actor(request), datetime.now(timezone.utc).isoformat()))
    audit(audit_actor(request), "grant_lab_template", template["source_project_id"], {"template_id": template_id, "username": target["username"]})
    return RedirectResponse(f"/projects/{template['source_project_id']}?message=Template+shared", status_code=303)


@app.post("/templates/{template_id}/access/{username}/remove")
async def revoke_template_access(request: Request, template_id: str, username: str):
    template = template_record(template_id)
    if not template or (not has_admin_scope(request) and template["owner"] != current_user(request)):
        raise HTTPException(404, "Lab template not found.")
    with sqlite3.connect(AUDIT_DB) as connection:
        connection.execute("DELETE FROM lab_template_access WHERE template_id=? AND subject_type='local_user' AND subject_id=?", (template_id, username))
    audit(audit_actor(request), "revoke_lab_template", template["source_project_id"], {"template_id": template_id, "username": username})
    return RedirectResponse(f"/projects/{template['source_project_id']}?message=Template+access+updated", status_code=303)


@app.post("/templates/{template_id}/unpublish")
async def unpublish_template(request: Request, template_id: str):
    template = template_record(template_id)
    if not template or (not has_admin_scope(request) and template["owner"] != current_user(request)):
        raise HTTPException(404, "Lab template not found.")
    with sqlite3.connect(AUDIT_DB) as connection:
        connection.execute("DELETE FROM lab_template_access WHERE template_id=?", (template_id,))
        connection.execute("DELETE FROM lab_templates WHERE template_id=?", (template_id,))
    archive_path = Path(str(template.get("archive_path") or ""))
    try:
        archive_path.resolve(strict=True).relative_to(LAB_TEMPLATE_DIR.resolve(strict=True))
        shutil.rmtree(archive_path.parent, ignore_errors=True)
    except (OSError, ValueError):
        pass
    audit(audit_actor(request), "unpublish_lab_template", template["source_project_id"], {"template_id": template_id, "name": template["name"]})
    return RedirectResponse("/?message=Lab+template+unpublished", status_code=303)


@app.post("/templates/{template_id}/approve")
async def approve_template(request: Request, template_id: str):
    require_admin(request)
    template = template_record(template_id)
    if not template:
        raise HTTPException(404, "Lab template not found.")
    if template.get("approval_status") == "approved":
        return RedirectResponse("/?message=Lab+template+is+already+approved", status_code=303)
    with sqlite3.connect(AUDIT_DB) as connection:
        connection.execute("UPDATE lab_templates SET approval_status='approved',approved_by=?,approved_at=? WHERE template_id=?", (audit_actor(request), datetime.now(timezone.utc).isoformat(), template_id))
    audit(audit_actor(request), "approve_lab_template", template["source_project_id"], {"template_id": template_id, "name": template["name"], "owner": template["owner"]})
    return RedirectResponse("/?message=Lab+template+approved+and+published", status_code=303)


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    if AUTH_ENABLED and not active_portal_session(request):
        return redirect_login()
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
        pending_source = templates_for_user(current_user(request), include_owned=has_admin_scope(request))
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
    for project, nodes in zip(projects, node_lists):
        project["is_running"] = any(
            not is_non_stoppable_node(node) and node.get("status") != "stopped"
            for node in nodes
        )
        project["access_lab_url"] = gns3_webui_project_url(project["project_id"])
        project["owner"] = lab_owner(project["project_id"]) or "Unassigned"
        project["shared_with"] = shared_users(project["project_id"])
        project["can_share"] = can_manage_lab_sharing(request, project["project_id"])
        project["lab_group"] = "user" if has_admin_scope(request) and project["owner"] not in {current_user(request), "Unassigned"} else "mine"
    return templates.TemplateResponse(request, "dashboard.html", {
        "projects": projects, "import_sources": import_sources, "shared_backup_sources": shared_backup_sources, "available_templates": available_templates, "pending_templates": pending_templates, "user": current_user(request),
        "signed_in_user": signed_in_user(request), "is_admin": is_admin(request), "can_manage_users": can_manage_users(request),
        "impersonating": request.session.get("impersonating", "") if AUTH_ENABLED else "",
        "portal_users": [account for account in list_users() if account.get("enabled")],
        "my_lab_count": sum(1 for project in projects if project["lab_group"] == "mine"),
        "user_lab_count": sum(1 for project in projects if project["lab_group"] == "user"),
        "retained_backups": retained_backups_for(None if has_admin_scope(request) else current_user(request)),
        "gns3_url": GNS3_URL, "audit": recent_audit(username=current_user(request), include_all=has_admin_scope(request)),
        "import_job_id": request.query_params.get("import_job", ""),
    })


@app.post("/projects")
async def create_project(request: Request, name: str = Form(...)):
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


@app.post("/projects/{project_id}/start")
async def start_lab(request: Request, project_id: str):
    user = current_user(request)
    require_project_access(request, project_id)
    if backup_is_active(project_id):
        return RedirectResponse("/?error=A+backup+is+running+for+this+lab.+Wait+for+it+to+finish+before+starting+nodes", status_code=303)
    try:
        started, ignored = await set_lab_node_power(project_id, "start")
    except HTTPException as exc:
        return RedirectResponse(f"/?error={quote_plus(str(exc.detail))}", status_code=303)
    audit(audit_actor(request), "start_lab", project_id, {"nodes_started": started, "non_stoppable_nodes": ignored})
    return RedirectResponse(f"/?message={quote_plus(f'Start requested for {started} lab node(s).')}", status_code=303)


@app.post("/projects/{project_id}/stop")
async def stop_lab(request: Request, project_id: str):
    user = current_user(request)
    require_project_access(request, project_id)
    if backup_is_active(project_id):
        return RedirectResponse("/?error=A+backup+is+running+for+this+lab.+Wait+for+it+to+finish+before+stopping+nodes", status_code=303)
    try:
        stopped, ignored = await set_lab_node_power(project_id, "stop")
    except HTTPException as exc:
        return RedirectResponse(f"/?error={quote_plus(str(exc.detail))}", status_code=303)
    audit(audit_actor(request), "stop_lab", project_id, {"nodes_stopped": stopped, "non_stoppable_nodes": ignored})
    return RedirectResponse(f"/?message={quote_plus(f'Stop requested for {stopped} lab node(s).')}", status_code=303)


@app.get("/projects/{project_id}", response_class=HTMLResponse)
async def project_page(request: Request, project_id: str):
    if AUTH_ENABLED and not active_portal_session(request):
        return redirect_login()
    try:
        require_project_access(request, project_id)
        project = await gns3("GET", f"/v2/projects/{project_id}")
        snapshots = await gns3("GET", f"/v2/projects/{project_id}/snapshots")
    except HTTPException as exc:
        return error_page(request, exc)
    snapshots, snapshot_total_bytes, storage_scanned = snapshot_storage(project, snapshots)
    snapshots.sort(key=lambda item: str(item.get("completed_at") or item.get("created_at") or ""))
    requested_job_id = request.query_params.get("backup_job", "")
    backup_job_id = requested_job_id if requested_job_id in app.state.backup_jobs else ""
    if not backup_job_id:
        backup_job_id = next((job_id for job_id, job in app.state.backup_jobs.items()
                              if job.get("project_id") == project_id and job.get("status") in ACTIVE_BACKUP_STATES), "")
    return templates.TemplateResponse(request, "project.html", {
        "project": project, "snapshots": snapshots, "snapshot_total_bytes": snapshot_total_bytes,
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
    """Create a new lab from a selected backup on the dashboard."""
    return await queue_snapshot_import(request, source_project_id, snapshot_id, name)


@app.post("/shared-backup-imports")
async def create_lab_from_shared_backup(request: Request, source_project_id: str = Form(...), snapshot_id: str = Form(...), name: str = Form(...)):
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
async def rename_project(request: Request, project_id: str, name: str = Form(...)):
    user = current_user(request)
    require_project_access(request, project_id)
    new_name = name.strip()
    if not new_name or len(new_name) > 80:
        return RedirectResponse("/?error=Use+a+lab+name+between+1+and+80+characters", status_code=303)
    try:
        project = await gns3("GET", f"/v2/projects/{project_id}")
        old_name = str(project.get("name", ""))
        if new_name == old_name:
            return RedirectResponse("/", status_code=303)
        await gns3("PUT", f"/v2/projects/{project_id}", json={"name": new_name})
    except HTTPException as exc:
        return RedirectResponse(f"/?error={quote_plus(str(exc.detail))}", status_code=303)
    audit(audit_actor(request), "rename_lab", project_id, {"old_name": old_name, "new_name": new_name})
    return RedirectResponse("/?message=Lab+renamed", status_code=303)


@app.post("/projects/{project_id}/snapshots/{snapshot_id}/restore")
async def restore_snapshot(request: Request, project_id: str, snapshot_id: str):
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
    user = current_user(request)
    require_project_access(request, project_id)
    try:
        await ensure_project_open(project_id)
        await gns3("DELETE", f"/v2/projects/{project_id}/snapshots/{snapshot_id}")
    except HTTPException as exc:
        return RedirectResponse(f"/projects/{project_id}?error={quote_plus(str(exc.detail))}", status_code=303)
    with sqlite3.connect(AUDIT_DB) as connection:
        connection.execute("DELETE FROM backup_shares WHERE project_id=? AND snapshot_id=?", (project_id, snapshot_id))
    audit(audit_actor(request), "delete_snapshot", project_id, {"snapshot_id": snapshot_id})
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@app.post("/projects/{project_id}/snapshots/delete")
async def delete_selected_snapshots(request: Request, project_id: str, snapshot_ids: list[str] = Form(...)):
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
        with sqlite3.connect(AUDIT_DB) as connection:
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
            with sqlite3.connect(AUDIT_DB) as connection:
                connection.execute("INSERT INTO retained_backups(backup_id,owner,source_project_id,source_lab_name,backup_name,archive_path,size_bytes,retained_at) VALUES(?,?,?,?,?,?,?,?)", (backup_id, user, project_id, str(project.get("name", "Lab")), str(snapshot.get("name", "Unnamed backup")), str(destination), destination.stat().st_size, datetime.now(timezone.utc).isoformat()))
            retained += 1
    await gns3("DELETE", f"/v2/projects/{project_id}")
    with sqlite3.connect(AUDIT_DB) as connection:
        connection.execute("DELETE FROM lab_owners WHERE project_id=?", (project_id,))
        connection.execute("DELETE FROM lab_shares WHERE project_id=?", (project_id,))
        connection.execute("DELETE FROM backup_shares WHERE project_id=?", (project_id,))
    audit(audit_actor(request), "delete_lab", project_id, {"name": project.get("name"), "retained_backups": retained})
    return RedirectResponse(f"/?message={quote_plus(f'Lab deleted. {retained} backup(s) retained.')}", status_code=303)
