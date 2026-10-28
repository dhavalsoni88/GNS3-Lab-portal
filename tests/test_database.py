import base64
import hashlib
import json
import os
import sqlite3
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

import app as portal
import portal_db as db

MARIADB = db.BACKEND == "mariadb"


@pytest.fixture(scope="module")
def client():
    with TestClient(portal.app) as test_client:
        test_client.headers["user-agent"] = "pytest-agent"
        yield test_client


def scrypt_hash(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def last_audit(action: str) -> dict:
    with db.connect() as connection:
        return dict(connection.execute("SELECT * FROM audit_log WHERE action=? ORDER BY id DESC LIMIT 1", (action,)).fetchone())


def login(client, username, password):
    return client.post("/login", data={"username": username, "password": password}, follow_redirects=False)


def test_translate_for_mariadb():
    sql = "INSERT INTO t(a,b) VALUES(?,?) ON CONFLICT(a) DO UPDATE SET b=excluded.b ORDER BY a COLLATE NOCASE LIKE '%x'"
    assert db.translate_for_mariadb(sql) == "INSERT INTO t(a,b) VALUES(%s,%s) ON DUPLICATE KEY UPDATE b=VALUES(b) ORDER BY a LIKE '%%x'"


def test_v8_scrypt_password_is_upgraded_to_argon2id(client):
    with db.connect() as connection:
        connection.execute("INSERT INTO portal_users(username,password_hash,role,enabled,created_at) VALUES(?,?,?,?,?)",
                           ("legacy", scrypt_hash("legacy-password-1"), "user", 1, "2026-01-01T00:00:00+00:00"))
    response = login(client, "legacy", "legacy-password-1")
    assert response.status_code == 303
    stored = portal.user_record("legacy")["password_hash"]
    assert stored.startswith("$argon2id$")
    assert portal.password_matches("legacy-password-1", stored)
    assert last_audit("password_rehashed")["actor"] == "legacy"
    client.post("/logout")
    assert login(client, "legacy", "legacy-password-1").status_code == 303
    client.post("/logout")


def test_login_audit_records_request_context(client):
    assert login(client, "portaladmin", "initial-admin-password").status_code == 303
    event = last_audit("login")
    assert event["actor"] == "portaladmin"
    assert event["outcome"] == "success"
    assert event["source_ip"] == "testclient"
    assert event["user_agent"] == "pytest-agent"
    assert len(event["request_id"]) == 32
    client.post("/logout")


def test_failed_login_is_audited_without_password(client):
    assert login(client, "portaladmin", "wrong-password-value").status_code == 401
    event = last_audit("login_failed")
    assert event["outcome"] == "failure"
    assert json.loads(event["details"]) == {"reason": "bad_password"}
    assert "wrong-password-value" not in json.dumps(event)


def test_duplicate_user_and_case_insensitive_usernames(client):
    login(client, "portaladmin", "initial-admin-password")
    form = {"username": "Alice", "password": "alice-password-1", "role": "user"}
    assert "message=User+created" in client.post("/admin/users", data=form, follow_redirects=False).headers["location"]
    form["username"] = "alice"
    assert "already+exists" in client.post("/admin/users", data=form, follow_redirects=False).headers["location"]
    assert portal.user_record("ALICE")["username"] == "Alice"
    client.post("/logout")


def test_upsert_lab_owner():
    portal.set_lab_owner("project-1", "Alice")
    portal.set_lab_owner("project-1", "legacy")
    assert portal.lab_owner("project-1") == "legacy"
    assert portal.owned_project_ids("LEGACY") == {"project-1"}


@pytest.mark.skipif(not MARIADB, reason="MariaDB audit triggers")
def test_audit_tables_are_append_only():
    import pymysql
    for statement in ("UPDATE audit_log SET actor='x'", "DELETE FROM audit_log", "DELETE FROM db_change_log"):
        with pytest.raises(pymysql.err.OperationalError, match="append-only"):
            with db.connect() as connection:
                connection.execute(statement)


@pytest.mark.skipif(not MARIADB, reason="MariaDB change-log triggers")
def test_change_log_records_actor_and_never_password_hash(client):
    login(client, "portaladmin", "initial-admin-password")
    client.post("/admin/users/Alice/update", data={"role": "user", "enabled": "1", "password": "alice-new-password", "retained_backup_limit": "3"})
    client.post("/logout")
    with db.connect() as connection:
        rows = connection.execute("SELECT * FROM db_change_log WHERE table_name='portal_users' AND row_key='Alice' ORDER BY id").fetchall()
    assert [row["operation"] for row in rows] == ["INSERT", "UPDATE"]
    update = rows[-1]
    assert update["portal_actor"] == "portaladmin"
    assert update["source_ip"] == "testclient"
    new_values = json.loads(update["new_values"])
    assert new_values["password_changed"] is True and new_values["retained_backup_limit"] == 3
    assert "password_hash" not in update["old_values"] + update["new_values"]


@pytest.mark.skipif(not MARIADB, reason="SQLite to MariaDB migration")
def test_sqlite_to_mariadb_migration(tmp_path):
    source = tmp_path / "v8.db"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE portal_users (username TEXT PRIMARY KEY COLLATE NOCASE, password_hash TEXT NOT NULL, role TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, email TEXT, first_name TEXT, last_name TEXT, avatar_filename TEXT, retained_backup_limit INTEGER NOT NULL DEFAULT 2)")
        connection.execute("INSERT INTO portal_users VALUES ('v8user', 'scrypt$a$b', 'user', 1, '2025-01-01T00:00:00+00:00', 'v8@example.com', NULL, NULL, NULL, 4)")
        connection.execute("CREATE TABLE lab_owners (project_id TEXT PRIMARY KEY, username TEXT NOT NULL, assigned_at TEXT NOT NULL)")
        connection.execute("INSERT INTO lab_owners VALUES ('p-v8', 'v8user', '2025-01-01T00:00:00+00:00')")
        connection.execute("CREATE TABLE audit_log (id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL, details TEXT)")
        connection.execute("INSERT INTO audit_log VALUES (900001, '2025-01-01T00:00:00+00:00', 'v8user', 'create_project', 'p-v8', '{}')")
    script = [sys.executable, "scripts/migrate-sqlite-to-mariadb.py", "--sqlite", str(source)]
    first = subprocess.run(script, capture_output=True, text=True, env=os.environ)
    assert first.returncode == 0, first.stdout + first.stderr
    assert "Migration verified" in first.stdout
    second = subprocess.run(script, capture_output=True, text=True, env=os.environ)
    assert second.returncode == 0 and "copied=     0" in second.stdout
    assert portal.user_record("v8user")["retained_backup_limit"] == 4
    assert portal.lab_owner("p-v8") == "v8user"
    with db.connect() as connection:
        assert connection.execute("SELECT actor FROM audit_log WHERE id=900001").fetchone()[0] == "v8user"
        assert connection.execute("SELECT portal_actor FROM db_change_log WHERE table_name='lab_owners' AND row_key='p-v8'").fetchone()[0] == "sqlite-migration"
    assert list(tmp_path.glob("v8.db.pre-mariadb-*"))


def test_dashboard_and_lab_sharing_with_stub_gns3(client):
    import httpx

    projects = [{"project_id": "project-1", "name": "Lab One", "status": "closed", "filename": "lab.gns3", "path": "/tmp/none"}]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/projects":
            return httpx.Response(200, json=projects)
        if request.url.path.rsplit("/", 1)[-1] in {"snapshots", "nodes", "links"}:
            return httpx.Response(200, json=[])
        if request.url.path == "/v2/computes":
            return httpx.Response(200, json=[{"compute_id": "local", "cpu_usage_percent": 12.5}])
        if request.url.path.startswith("/v2/"):
            return httpx.Response(200, json={"compute_id": "local", "cpu_usage_percent": 12.5})
        return httpx.Response(404, json={})

    original = portal.app.state.gns3_client
    portal.app.state.gns3_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        login(client, "portaladmin", "initial-admin-password")
        portal.set_lab_owner("project-1", "portaladmin")
        share = client.post("/projects/project-1/shares", data={"username": "alice"}, follow_redirects=False)
        assert share.status_code == 303
        client.post("/projects/project-1/shares", data={"username": "Alice"}, follow_redirects=False)
        assert portal.shared_users("project-1") == ["Alice"]
        page = client.get("/")
        assert page.status_code == 200 and "Lab One" in page.text
        assert client.get("/admin/system-status").json()["cpu_percent"] == 12.5
        assert client.get("/admin/users").status_code == 200
        client.post("/logout")
    finally:
        portal.app.state.gns3_client = original
