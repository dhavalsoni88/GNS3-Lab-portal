"""Who may see and change which lab, and the sign-in and start-up safeguards."""
import os
import subprocess
import sys

import httpx
import pytest
from fastapi.testclient import TestClient

import app as portal
import portal_db as db

PROJECTS = [{"project_id": "lab-owned", "name": "Owner Lab", "status": "opened"}]


def gns3_stub(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/v2/projects":
        return httpx.Response(200, json=PROJECTS)
    if request.url.path.endswith(("/snapshots", "/nodes")):
        return httpx.Response(200, json=[])
    if request.url.path.startswith("/v2/projects/"):
        return httpx.Response(200, json=PROJECTS[0])
    return httpx.Response(404, json={})


@pytest.fixture(scope="module")
def client():
    with TestClient(portal.app) as test_client:
        portal.app.state.gns3_client = httpx.AsyncClient(transport=httpx.MockTransport(gns3_stub))
        login(test_client, "portaladmin", "initial-admin-password")
        for name in ("owner1", "other1"):
            test_client.post("/admin/users", data={"username": name, "password": f"{name}-password-1", "roles": ["lab_editor"]})
        test_client.post("/logout")
        portal.set_lab_owner("lab-owned", "owner1")
        yield test_client


def login(client, username, password):
    return client.post("/login", data={"username": username, "password": password}, follow_redirects=False)


def test_other_user_cannot_see_or_delete_a_lab_until_it_is_shared(client):
    login(client, "other1", "other1-password-1")
    assert client.get("/projects/lab-owned").status_code == 404
    assert "Owner Lab" not in client.get("/").text
    assert client.post("/projects/lab-owned/delete", data={"confirmation": "Owner Lab"}).status_code == 404
    client.post("/logout")

    login(client, "owner1", "owner1-password-1")
    client.post("/projects/lab-owned/shares", data={"username": "other1"})
    client.post("/logout")

    login(client, "other1", "other1-password-1")
    assert client.get("/projects/lab-owned").status_code == 200
    assert "Owner Lab" in client.get("/").text
    # Shared users may open the lab but never delete it.
    assert client.post("/projects/lab-owned/delete", data={"confirmation": "Owner Lab"}).status_code == 403
    client.post("/logout")


def test_repeated_failed_sign_ins_are_locked_out(client):
    for _ in range(portal.LOGIN_FAILURE_LIMIT):
        assert login(client, "owner1", "wrong-password").status_code == 401
    assert login(client, "owner1", "owner1-password-1").status_code == 429
    portal.app.state.login_failures.clear()
    assert login(client, "owner1", "owner1-password-1").status_code == 303
    client.post("/logout")


def test_impersonation_ends_when_the_admin_loses_the_right(client):
    login(client, "portaladmin", "initial-admin-password")
    client.post("/admin/users", data={"username": "admin2", "password": "admin2-password-1", "roles": ["platform_admin"]})
    client.post("/logout")
    login(client, "admin2", "admin2-password-1")
    client.post("/admin/impersonate/owner1")
    assert client.get("/projects/lab-owned").status_code == 200
    import portal_auth as auth
    auth.set_direct_roles("admin2", {"viewer"})
    assert client.get("/projects/lab-owned").status_code == 404
    client.post("/logout")


def test_retained_backup_can_be_deleted_by_its_owner(client):
    archive = portal.RETAINED_BACKUP_DIR / "lab-gone" / "kept.gns3snapshot"
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_bytes(b"PK")
    with db.connect() as connection:
        connection.execute("INSERT INTO retained_backups(backup_id,owner,source_project_id,source_lab_name,backup_name,archive_path,size_bytes,retained_at) VALUES(?,?,?,?,?,?,?,?)",
                           ("kept-1", "owner1", "lab-gone", "Gone", "Before", str(archive), 2, "2026-01-01T00:00:00+00:00"))
    login(client, "other1", "other1-password-1")
    client.post("/retained-backups/delete", data={"backup_id": "kept-1"})
    assert archive.exists()
    client.post("/logout")
    login(client, "owner1", "owner1-password-1")
    client.post("/retained-backups/delete", data={"backup_id": "kept-1"})
    assert not archive.exists() and portal.retained_backup_count("owner1") == 0
    client.post("/logout")


def test_placeholder_secret_key_stops_start_up(monkeypatch):
    monkeypatch.setattr(portal, "SECRET_KEY", "replace-with-a-long-random-value")
    with pytest.raises(RuntimeError, match="PORTAL_SECRET_KEY"):
        portal.init_db()


def test_empty_mariadb_with_old_sqlite_file_refuses_to_start(tmp_path):
    raw = db._connect_mariadb(database=None)
    with raw.cursor() as cursor:
        cursor.execute("DROP DATABASE IF EXISTS gns3_portal_gate_test")
        cursor.execute("CREATE DATABASE gns3_portal_gate_test CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
    raw.close()
    legacy = tmp_path / "audit.db"
    legacy.write_bytes(b"")
    env = {**os.environ, "PORTAL_DB_NAME": "gns3_portal_gate_test", "AUDIT_DB": str(legacy)}
    result = subprocess.run([sys.executable, "-c", "import app; app.init_db()"], capture_output=True, text=True, env=env)
    assert result.returncode != 0 and "migrate-sqlite-to-mariadb" in result.stderr
    old = subprocess.run([sys.executable, "-c", "import app"], capture_output=True, text=True, env={**env, "PORTAL_DB_BACKEND": "sqlite"})
    assert old.returncode != 0 and "SQLite is no longer supported" in old.stderr


def test_delete_lab_keeps_backups_only_when_the_whole_delete_succeeds(client, tmp_path, monkeypatch):
    snapshots_dir = tmp_path / "projects" / "lab-del" / "snapshots"
    snapshots_dir.mkdir(parents=True)
    (snapshots_dir / "s1.gns3snapshot").write_bytes(b"PK1")
    project = {"project_id": "lab-del", "name": "Delete Me", "path": str(snapshots_dir.parent), "status": "opened"}
    snapshots = [{"snapshot_id": "s1", "name": "s1"}, {"snapshot_id": "s2", "name": "s2"}]
    deletes, refuse_delete = [], {"on": False}

    def stub(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            deletes.append(request.url.path)
            return httpx.Response(409, json={"message": "project locked"}) if refuse_delete["on"] else httpx.Response(204)
        if request.url.path.endswith("/snapshots"):
            return httpx.Response(200, json=snapshots)
        return httpx.Response(200, json=project)

    monkeypatch.setattr(portal, "GNS3_PROJECTS_DIR", tmp_path / "projects")
    original_client = portal.app.state.gns3_client
    portal.app.state.gns3_client = httpx.AsyncClient(transport=httpx.MockTransport(stub))
    portal.set_lab_owner("lab-del", "owner1")
    form = {"confirmation": "Delete Me", "retained_snapshot_ids": ["s1", "s2"]}
    login(client, "owner1", "owner1-password-1")
    before = portal.retained_backup_count("owner1")

    # s2 has no file: nothing is kept and GNS3 is never asked to delete.
    assert "error=" in client.post("/projects/lab-del/delete", data=form, follow_redirects=False).headers["location"]
    assert portal.retained_backup_count("owner1") == before and deletes == []

    # Both files exist but GNS3 refuses: the kept copies are undone.
    (snapshots_dir / "s2.gns3snapshot").write_bytes(b"PK2")
    refuse_delete["on"] = True
    assert "error=" in client.post("/projects/lab-del/delete", data=form, follow_redirects=False).headers["location"]
    assert portal.retained_backup_count("owner1") == before and len(deletes) == 1
    assert not any((portal.RETAINED_BACKUP_DIR / "lab-del").glob("*"))
    assert portal.lab_owner("lab-del") == "owner1"

    refuse_delete["on"] = False
    assert "message=" in client.post("/projects/lab-del/delete", data=form, follow_redirects=False).headers["location"]
    assert portal.retained_backup_count("owner1") == before + 2 and portal.lab_owner("lab-del") is None
    client.post("/logout")
    portal.app.state.gns3_client = original_client


def test_password_change_needs_current_password_and_reset_signs_user_out(client):
    other_device = TestClient(portal.app)  # no lifespan: shares the running app and its sessions
    login(client, "owner1", "owner1-password-1")
    form = {"new_password": "owner1-password-2", "confirm_new_password": "owner1-password-2"}
    wrong = client.post("/profile", data={**form, "current_password": "not-it"}, follow_redirects=False)
    assert "Current+password+is+incorrect" in wrong.headers["location"]
    right = client.post("/profile", data={**form, "current_password": "owner1-password-1"}, follow_redirects=False)
    assert "message=" in right.headers["location"]
    assert client.get("/profile", follow_redirects=False).status_code == 200  # still signed in
    client.post("/logout")

    # An admin reset also signs the user out everywhere.
    login(other_device, "owner1", "owner1-password-2")
    login(client, "portaladmin", "initial-admin-password")
    client.post("/admin/users/owner1/update", data={"roles": ["lab_editor"], "enabled": "1", "password": "owner1-password-1", "retained_backup_limit": "2"})
    assert other_device.get("/profile", follow_redirects=False).status_code == 303
    client.post("/logout")


def test_cross_site_posts_are_refused_but_saml_responses_are_not(client):
    assert client.post("/logout", headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post("/logout", headers={"Origin": "http://testserver"}, follow_redirects=False).status_code == 303
    # The identity provider posts from its own site; SAML signatures protect that route.
    assert client.post("/auth/saml/none/acs", headers={"Origin": "https://idp.example"}).status_code == 404


def test_dot_segments_never_reach_the_gns3_api():
    import asyncio
    from fastapi import HTTPException
    for path in ("/v2/projects/lab-1/snapshots/..", "/v2/projects/./x", "/v2/projects/a?b=1"):
        with pytest.raises(HTTPException) as refused:
            asyncio.run(portal.gns3("DELETE", path))
        assert refused.value.status_code == 400


def test_import_job_status_is_only_for_the_person_who_started_it(client):
    portal.app.state.import_jobs["job-owner1"] = {"actor": "owner1", "source_project_id": "", "status": "importing", "progress_percent": 50, "message": "", "project_id": None}
    login(client, "other1", "other1-password-1")
    assert client.get("/backup-import-jobs/job-owner1").status_code == 404
    client.post("/logout")
    login(client, "owner1", "owner1-password-1")
    assert client.get("/backup-import-jobs/job-owner1").status_code == 200
    client.post("/logout")


def test_saml_sign_in_starts_are_capped():
    import portal_auth as auth
    store = auth.SamlRequestStore()
    store.MAX_PENDING = 3
    for n in range(3):
        store.put(f"relay-{n}", "idp", f"id-{n}")
    with pytest.raises(auth.IdentityError):
        store.put("relay-3", "idp", "id-3")


def test_stopping_a_closed_lab_never_opens_it(client):
    calls = []

    def stub(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("/open"):
            return httpx.Response(409, json={"message": "The image winserver2019.qcow2 is missing", "status": 409})
        if request.url.path.endswith("/nodes"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={"project_id": "lab-owned", "name": "Owner Lab", "status": "closed"})

    original = portal.app.state.gns3_client
    portal.app.state.gns3_client = httpx.AsyncClient(transport=httpx.MockTransport(stub))
    try:
        login(client, "owner1", "owner1-password-1")
        stop = client.post("/projects/lab-owned/stop", data={"return_to": "/projects/lab-owned"}, follow_redirects=False)
        assert "message=" in stop.headers["location"] and not any(path.endswith("/open") for _, path in calls)
        # Starting still has to open it, and shows GNS3's own reason when that fails.
        start = client.post("/projects/lab-owned/start", data={"return_to": "/projects/lab-owned"}, follow_redirects=False)
        assert "winserver2019.qcow2+is+missing" in start.headers["location"]
        client.post("/logout")
    finally:
        portal.app.state.gns3_client = original
