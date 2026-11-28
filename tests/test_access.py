"""Who may see and change which lab, and the sign-in and start-up safeguards."""
import os
import subprocess
import sys
from pathlib import Path

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
        # Starting still opens it; the failure is reported without GNS3's own text (which can hold server paths).
        start = client.post("/projects/lab-owned/start", data={"return_to": "/projects/lab-owned"}, follow_redirects=False)
        assert "HTTP+409" in start.headers["location"] and "winserver2019" not in start.headers["location"]
        client.post("/logout")
    finally:
        portal.app.state.gns3_client = original


def test_lab_limit_is_checked_before_any_lab_is_created(client):
    created = []

    def stub(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v2/projects":
            created.append(request.url.path)
            return httpx.Response(201, json={"project_id": f"new-{len(created)}", "name": "New", "path": "/tmp/none"})
        if request.url.path.startswith("/v2/projects/"):
            return httpx.Response(200, json={"project_id": request.url.path.rsplit("/", 1)[-1], "name": "New", "path": "/tmp/none"})
        return httpx.Response(200, json=[])

    original = portal.app.state.gns3_client
    portal.app.state.gns3_client = httpx.AsyncClient(transport=httpx.MockTransport(stub))
    try:
        login(client, "portaladmin", "initial-admin-password")
        owned = len(portal.owned_project_ids("other1"))
        client.post("/admin/users/other1/update", data={"roles": ["lab_editor"], "enabled": "1", "retained_backup_limit": "2", "lab_limit": str(owned)})
        client.post("/logout")
        login(client, "other1", "other1-password-1")
        assert f"of {owned} labs" in client.get("/labs/new").text
        refused = client.post("/projects", data={"name": "One too many"}, follow_redirects=False)
        assert "limit" in refused.headers["location"] and created == []          # GNS3 never asked
        refused_import = client.post("/retained-backup-imports", data={"backup_id": "none", "name": "x"}, follow_redirects=False)
        assert "error=" in refused_import.headers["location"] and created == []
        client.post("/logout")

        login(client, "portaladmin", "initial-admin-password")
        client.post("/admin/users/other1/update", data={"roles": ["lab_editor"], "enabled": "1", "retained_backup_limit": "2", "lab_limit": str(owned + 1)})
        # A form without the field keeps the limit instead of resetting it.
        client.post("/admin/users/other1/update", data={"roles": ["lab_editor"], "enabled": "1", "retained_backup_limit": "2"})
        assert portal.lab_limit("other1") == owned + 1
        client.post("/logout")
        login(client, "other1", "other1-password-1")
        assert "/projects/new-1" in client.post("/projects", data={"name": "Allowed now"}, follow_redirects=False).headers["location"]
        client.post("/logout")
    finally:
        portal.app.state.gns3_client = original


def test_template_is_copied_in_the_background_with_progress(client, tmp_path, monkeypatch):
    import time
    snapshots_dir = tmp_path / "projects" / "lab-owned" / "snapshots"
    snapshots_dir.mkdir(parents=True)
    archive = b"PK" + b"x" * (3 * 1024 * 1024)
    (snapshots_dir / "base.gns3snapshot").write_bytes(archive)
    project = {"project_id": "lab-owned", "name": "Owner Lab", "path": str(snapshots_dir.parent), "status": "opened"}

    def stub(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/snapshots"):
            return httpx.Response(200, json=[{"snapshot_id": "snap-base", "name": "base"}])
        if request.url.path.endswith("/nodes"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=project)

    monkeypatch.setattr(portal, "GNS3_PROJECTS_DIR", tmp_path / "projects")
    original = portal.app.state.gns3_client
    portal.app.state.gns3_client = httpx.AsyncClient(transport=httpx.MockTransport(stub))
    try:
        login(client, "owner1", "owner1-password-1")
        started = client.post("/projects/lab-owned/snapshots/snap-base/template", data={"name": "Base topology"}, follow_redirects=False)
        location = started.headers["location"]
        assert "template_job=" in location            # the request returns at once instead of copying
        job_id = location.split("template_job=")[1].split("#")[0]
        assert "data-template-job-url" in client.get(location.split("#")[0]).text
        for _ in range(100):
            job = client.get(f"/projects/lab-owned/template-jobs/{job_id}").json()
            if job["status"] != "copying":
                break
            time.sleep(0.05)
        assert job["status"] == "completed" and job["progress_percent"] == 100
        template = next(item for item in portal.templates_for_user("owner1") if item["snapshot_id"] == "snap-base")
        assert template["approval_status"] == "pending"   # a Lab Editor's template waits for approval
        assert Path(template["archive_path"]).read_bytes() == archive
        # Asking again is refused before any copy starts.
        again = client.post("/projects/lab-owned/snapshots/snap-base/template", data={"name": "Again"}, follow_redirects=False)
        assert "already+a+template" in again.headers["location"]
        client.post("/logout")
        # Another user cannot read the job.
        login(client, "other1", "other1-password-1")
        assert client.get(f"/projects/lab-owned/template-jobs/{job_id}").status_code == 404
        client.post("/logout")
    finally:
        portal.app.state.gns3_client = original


@pytest.mark.parametrize("project,expected", [
    ({"status": "closed"}, (0, 0)), ({"status": "OPENED"}, (1, 0)),
    ({"status": "closing"}, None), ({}, None), ({"status": 42}, None), ([], None), (None, None),
])
def test_stop_only_skips_an_explicitly_closed_project(monkeypatch, project, expected):
    import asyncio
    from fastapi import HTTPException
    calls = []

    async def fake_gns3(method, path, **kwargs):
        calls.append((method, path))
        if path.endswith("/nodes") and method == "GET":
            return [{"node_id": "vm", "node_type": "qemu", "status": "started"}]
        return project if method == "GET" else None

    monkeypatch.setattr(portal, "gns3", fake_gns3)
    if expected is None:
        with pytest.raises(HTTPException) as refused:
            asyncio.run(portal.set_lab_node_power("lab-1", "stop"))
        assert refused.value.status_code == 502 and calls == [("GET", "/v2/projects/lab-1")]
    else:
        assert asyncio.run(portal.set_lab_node_power("lab-1", "stop")) == expected
    assert ("POST", "/v2/projects/lab-1/open") not in calls


def test_gns3_error_text_never_reaches_users():
    import asyncio
    from fastapi import HTTPException
    body = {"message": "Cannot open /home/gns3/GNS3/projects/secret.gns3 (password example-secret)"}
    original = getattr(portal.app.state, "gns3_client", None)
    portal.app.state.gns3_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(409, json=body)))
    try:
        with pytest.raises(HTTPException) as refused:
            asyncio.run(portal.gns3("GET", "/v2/projects/lab-1"))
    finally:
        portal.app.state.gns3_client = original
    assert refused.value.detail == "GNS3 refused the request (HTTP 409). An administrator can find details in gns3-api.log."


# --- appliance publishing ---------------------------------------------------

DISK = b"QFI\xfb fake windows disk"


def appliance_file(disk=DISK, name="winserver2019.qcow2", **qemu):
    import hashlib
    return {
        "name": "Win Server", "category": "guest", "registry_version": 4, "usage": "Notes that came with the file",
        "qemu": {"adapter_type": "e1000", "adapters": 2, "ram": 4096, "arch": "x86_64", "console_type": "vnc", "kvm": "require", **qemu},
        "images": [{"filename": name, "version": "2019", "md5sum": hashlib.md5(disk).hexdigest(), "filesize": len(disk)}],
        "versions": [{"name": "2019", "images": {"hda_disk_image": name}}],
    }


@pytest.fixture
def appliances(tmp_path, monkeypatch, client):
    """An empty appliance store, a place for disks that already exist, and a GNS3 that records templates."""
    import json as jsonlib
    store, elsewhere = tmp_path / "images" / "QEMU", tmp_path / "images" / "old"
    store.mkdir(parents=True)
    elsewhere.mkdir()
    monkeypatch.setattr(portal, "GNS3_IMAGES_DIR", store)
    monkeypatch.setattr(portal, "APPLIANCE_SEARCH_DIRS", (tmp_path / "images",))
    state = {"templates": [{"name": "Existing router", "template_type": "qemu", "category": "router"}], "posted": [], "store": store, "elsewhere": elsewhere,
             "binaries": [{"path": "/usr/bin/qemu-system-aarch64"}, {"path": "/usr/bin/qemu-system-x86_64"}]}
    monkeypatch.setattr(portal.shutil, "which", lambda name: None)   # never depend on the machine running the tests

    def stub(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/templates" and request.method == "POST":
            state["posted"].append(jsonlib.loads(request.content))
            return httpx.Response(201, json={})
        if request.url.path == "/v2/templates":
            return httpx.Response(200, json=state["templates"])
        if request.url.path == "/v2/computes/local/qemu/binaries":
            return httpx.Response(200, json=state["binaries"])
        return httpx.Response(404, json={})

    original = portal.app.state.gns3_client
    portal.app.state.gns3_client = httpx.AsyncClient(transport=httpx.MockTransport(stub))
    login(client, "portaladmin", "initial-admin-password")
    yield state
    client.post("/logout")
    portal.app.state.gns3_client = original


def check_appliance(client, appliance=None):
    import json as jsonlib
    response = client.post("/admin/appliances", files={"appliance_file": ("win.gns3a", jsonlib.dumps(appliance or appliance_file()).encode(), "application/json")}, follow_redirects=False)
    assert response.status_code == 303, response.text
    return response.headers["location"]


def wait_for_job(client, location):
    import time
    page = client.get(location)
    job_url = page.text.split('data-appliance-job-url="')[1].split('"')[0]
    for _ in range(200):
        job = client.get(job_url).json()
        if job["status"] != "working":
            return job
        time.sleep(0.05)
    raise AssertionError("appliance job did not finish")


def test_appliance_review_says_where_each_disk_is_and_where_a_missing_one_goes(client, appliances):
    review = check_appliance(client)
    page = client.get(review).text
    store = appliances["store"]
    assert str(store) in page and "Missing" in page and f"{store}/winserver2019.qcow2" in page
    assert "Every disk has to be in the store" in page                     # nothing to publish yet

    (appliances["elsewhere"] / "winserver2019.qcow2").write_bytes(DISK)
    page = client.get(review).text
    assert "Found elsewhere" in page and f"{appliances['elsewhere']}/winserver2019.qcow2" in page and "will be moved" in page

    (store / "winserver2019.qcow2").write_bytes(DISK + b"more")            # same name, different size
    assert "Does not match" in client.get(review).text
    (store / "winserver2019.qcow2").write_bytes(DISK)
    assert "In the store" in client.get(review).text and "Every disk has to be in the store" not in client.get(review).text


def test_appliance_disk_is_streamed_into_the_store_and_checked(client, appliances):
    review = check_appliance(client)
    upload = review.replace("/admin/appliances/", "/admin/appliances/") + "/images/winserver2019.qcow2"
    store = appliances["store"]
    assert client.put(upload, content=b"x" * len(DISK)).status_code == 422                 # right size, wrong checksum
    assert client.put(upload, content=DISK + b"1").status_code == 422                        # wrong size
    assert list(store.iterdir()) == []                                                       # nothing left behind
    assert client.put(review + "/images/other.qcow2", content=DISK).status_code == 404      # only the disks this appliance names
    assert client.put(upload, content=DISK).status_code == 200
    assert (store / "winserver2019.qcow2").read_bytes() == DISK
    assert client.put(upload, content=DISK).status_code == 409                               # never replaced
    assert client.put(upload, content=DISK, headers={"Origin": "https://evil.example"}).status_code == 403
    client.post("/logout")
    login(client, "owner1", "owner1-password-1")
    assert client.put(upload, content=DISK).status_code == 403                               # only Platform Admins
    client.post("/logout")
    login(client, "portaladmin", "initial-admin-password")


def test_publishing_moves_found_disks_and_registers_the_template_with_notes(client, appliances):
    review = check_appliance(client)
    assert client.post(review + "/publish", data={}, follow_redirects=False).headers["location"].count("Not+ready") == 1   # disk missing
    source = appliances["elsewhere"] / "winserver2019.qcow2"
    source.write_bytes(DISK)
    started = client.post(review + "/publish", data={"notes": "Login: administrator. Set the password on first boot."}, follow_redirects=False)
    job = wait_for_job(client, started.headers["location"])
    assert job["status"] == "completed" and job["progress_percent"] == 100
    assert not source.exists() and (appliances["store"] / "winserver2019.qcow2").read_bytes() == DISK     # moved, not copied
    template = appliances["posted"][0]
    assert template["name"] == "Win Server 2019" and template["hda_disk_image"] == "winserver2019.qcow2"
    assert template["usage"] == "Login: administrator. Set the password on first boot."                # shown under Usage in GNS3
    assert template["ram"] == 4096 and template["platform"] == "x86_64" and "kvm" not in template
    assert template["qemu_path"] == "/usr/bin/qemu-system-x86_64"             # without it GNS3 warns "Could not find '' in the Qemu binaries list"
    assert "Existing router" in [item["name"] for item in appliances["templates"]]

    appliances["templates"].append({"name": "Win Server 2019"})                                          # same name: refused
    assert "already+exists" in client.post(review + "/publish", data={}, follow_redirects=False).headers["location"]
    assert len(appliances["posted"]) == 1


def test_a_found_disk_with_the_wrong_checksum_is_left_where_it_is(client, appliances):
    review = check_appliance(client)
    source = appliances["elsewhere"] / "winserver2019.qcow2"
    source.write_bytes(b"y" * len(DISK))                                                                 # same size, different content
    job = wait_for_job(client, client.post(review + "/publish", data={}, follow_redirects=False).headers["location"])
    assert job["status"] == "failed" and "checksum" in job["message"]
    assert source.exists() and not (appliances["store"] / "winserver2019.qcow2").exists() and appliances["posted"] == []


def test_only_safe_qemu_options_are_accepted(client, appliances):
    ok = check_appliance(client, appliance_file(options="-nographic -cpu host -smp 2"))
    assert ok.startswith("/admin/appliances/")
    refused = client.post("/admin/appliances", files={"appliance_file": ("w.gns3a", __import__("json").dumps(appliance_file(options="-nographic -virtfs local,path=/,mount_tag=h,security_model=none")).encode(), "application/json")}, follow_redirects=False)
    assert "-virtfs" in refused.headers["location"] and "/admin/appliances/" not in refused.headers["location"].split("?")[0][len("/admin/appliances"):]


# --- review findings -------------------------------------------------------------

def test_simultaneous_requests_cannot_exceed_the_lab_limit(client):
    import asyncio
    created = []

    async def slow_stub(request):
        if request.method == "POST" and request.url.path == "/v2/projects":
            await asyncio.sleep(0.3)
            created.append(1)
            return httpx.Response(201, json={"project_id": f"race-{len(created)}", "name": "R", "path": "/tmp/none"})
        if request.url.path.startswith("/v2/projects/"):
            return httpx.Response(200, json={"project_id": "x", "name": "R", "path": "/tmp/none"})
        return httpx.Response(200, json=[])

    original = portal.app.state.gns3_client
    portal.app.state.gns3_client = httpx.AsyncClient(transport=httpx.MockTransport(slow_stub))
    try:
        login(client, "portaladmin", "initial-admin-password")
        client.post("/admin/users", data={"username": "racer1", "password": "racer1-password-1", "roles": ["lab_editor"]})
        client.post("/logout")
        login(client, "racer1", "racer1-password-1")
        cookies = dict(client.cookies)

        async def go():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=portal.app), base_url="http://testserver", cookies=cookies) as many:
                return await asyncio.gather(*[many.post("/projects", data={"name": f"R{i}"}) for i in range(6)])

        asyncio.run(go())
        assert len(created) == portal.lab_limit("racer1") == 2          # six at once, two allowed
        assert portal.app.state.lab_reservations["racer1"] == 0         # slots are released afterwards
    finally:
        portal.app.state.gns3_client = original


def test_labs_deleted_in_gns3_do_not_count_against_the_limit(client):
    projects = []

    def stub(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v2/projects":
            return httpx.Response(201, json={"project_id": "fresh", "name": "F", "path": "/tmp/none"})
        if request.url.path == "/v2/projects":
            return httpx.Response(200, json=projects)
        return httpx.Response(200, json={"project_id": "fresh", "name": "F", "path": "/tmp/none"})

    original = portal.app.state.gns3_client
    portal.app.state.gns3_client = httpx.AsyncClient(transport=httpx.MockTransport(stub))
    try:
        login(client, "portaladmin", "initial-admin-password")
        client.post("/admin/users", data={"username": "ghost1", "password": "ghost1-password-1", "roles": ["lab_editor"]})
        client.post("/logout")
        for ghost in ("ghost-a", "ghost-b"):
            portal.set_lab_owner(ghost, "ghost1")
        login(client, "ghost1", "ghost1-password-1")
        assert "/projects/fresh" in client.post("/projects", data={"name": "Allowed"}, follow_redirects=False).headers["location"]   # both owned labs are gone from GNS3
        projects[:] = [{"project_id": "ghost-a"}, {"project_id": "ghost-b"}]                                                      # now they exist
        assert "limit" in client.post("/projects", data={"name": "Too many"}, follow_redirects=False).headers["location"]
        client.post("/logout")
    finally:
        portal.app.state.gns3_client = original


def test_origin_check_refuses_null_origins_and_ports_the_proxy_dropped(client, monkeypatch):
    monkeypatch.setattr(portal, "PORTAL_PUBLIC_URL", "")
    assert client.post("/logout", headers={"Origin": "null"}).status_code == 403
    assert client.post("/logout", headers={"Origin": "https://lab.example.com:8443", "Host": "lab.example.com"}).status_code == 403
    assert client.post("/logout", headers={"Origin": "https://lab.example.com:8443", "Host": "lab.example.com:8443"}, follow_redirects=False).status_code == 303


def test_half_copied_template_folders_are_cleaned_up_at_start(tmp_path, monkeypatch):
    import os
    import time
    root = tmp_path / "templates"
    root.mkdir()
    monkeypatch.setattr(portal, "LAB_TEMPLATE_DIR", root)
    old_orphan, new_orphan, other = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222", "my-own-folder"
    for name in (old_orphan, new_orphan, other):
        (root / name).mkdir()
    for name in (old_orphan, other):
        os.utime(root / name, (time.time() - 3 * 86400,) * 2)
    portal.remove_orphan_template_dirs()
    assert {path.name for path in root.iterdir()} == {other, new_orphan}   # only the old, UUID-named orphan went


def test_a_disk_on_another_filesystem_is_copied_checked_and_then_removed(client, appliances, monkeypatch):
    import os
    import shutil
    import tempfile
    if not os.path.isdir("/dev/shm") or os.stat("/dev/shm").st_dev == os.stat(appliances["elsewhere"]).st_dev:
        pytest.skip("needs a second filesystem")
    other_disk_store = Path(tempfile.mkdtemp(dir="/dev/shm"))
    try:
        monkeypatch.setattr(portal, "GNS3_IMAGES_DIR", other_disk_store)
        review = check_appliance(client)
        source = appliances["elsewhere"] / "winserver2019.qcow2"
        source.write_bytes(DISK)
        job = wait_for_job(client, client.post(review + "/publish", data={}, follow_redirects=False).headers["location"])
        assert job["status"] == "completed"
        assert (other_disk_store / "winserver2019.qcow2").read_bytes() == DISK and not source.exists()
        assert [path.name for path in other_disk_store.iterdir()] == ["winserver2019.qcow2"]     # no .part file left
    finally:
        shutil.rmtree(other_disk_store, ignore_errors=True)


def test_default_template_name_does_not_repeat_the_version():
    base = appliance_file()
    base["name"], base["versions"][0]["name"] = "WebGoat on Ubuntu 26.04 gold-v1", "gold-v1"
    assert portal.appliance_template(base, "")[0]["name"] == "WebGoat on Ubuntu 26.04 gold-v1"      # version already in the name
    base["name"] = "WebGoat"
    assert portal.appliance_template(base, "")[0]["name"] == "WebGoat gold-v1"                      # version added when it is not


def test_template_name_can_be_chosen_when_publishing(client, appliances):
    review = check_appliance(client)
    (appliances["store"] / "winserver2019.qcow2").write_bytes(DISK)
    assert "Template name" in client.get(review).text and 'value="Win Server 2019"' in client.get(review).text
    appliances["templates"].append({"name": "Taken name"})
    assert "Choose+another+name" in client.post(review + "/publish", data={"name": "Taken name"}, follow_redirects=False).headers["location"]
    assert "200+characters" in client.post(review + "/publish", data={"name": "x" * 201}, follow_redirects=False).headers["location"]
    assert appliances["posted"] == []
    job = wait_for_job(client, client.post(review + "/publish", data={"name": "  Windows lab server  "}, follow_redirects=False).headers["location"])
    assert job["status"] == "completed" and "Windows lab server" in job["message"]
    assert appliances["posted"][0]["name"] == "Windows lab server"                                   # trimmed, and used as typed


def test_qemu_program_comes_from_gns3_then_the_server_and_is_shown(client, appliances, monkeypatch):
    review = check_appliance(client)
    assert "/usr/bin/qemu-system-x86_64" in client.get(review).text
    appliances["binaries"] = []                                                                     # GNS3 lists none: fall back to the server's PATH
    monkeypatch.setattr(portal.shutil, "which", lambda name: f"/opt/qemu/bin/{name}")
    assert "/opt/qemu/bin/qemu-system-x86_64" in client.get(review).text
    monkeypatch.setattr(portal.shutil, "which", lambda name: None)                                  # nowhere: say so instead of publishing blind
    page = client.get(review).text
    assert "No <code>qemu-system-x86_64</code> was found" in page
    (appliances["store"] / "winserver2019.qcow2").write_bytes(DISK)
    wait_for_job(client, client.post(review + "/publish", data={}, follow_redirects=False).headers["location"])
    assert "qemu_path" not in appliances["posted"][0]
