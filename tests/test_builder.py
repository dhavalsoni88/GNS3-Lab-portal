"""Appliance builder: job validation, the portal pages, and the builder service run against stub tools."""
import hashlib
import importlib.util
import json
import os
import stat
import sys

import pytest
from fastapi.testclient import TestClient

import app as portal
import portal_builder as pb

PNG = pb.PNG_MAGIC + b"fake"


@pytest.fixture
def base(tmp_path, monkeypatch):
    (tmp_path / "baselines").mkdir()
    (tmp_path / "baselines" / "ubuntu-base.qcow2").write_bytes(b"BASELINE-DISK")
    (tmp_path / "images").mkdir()
    monkeypatch.setattr(portal, "BUILDER_DIR", tmp_path)
    monkeypatch.setattr(portal, "GNS3_IMAGES_DIR", tmp_path / "images")
    return tmp_path


def form(**extra):
    return {"recipe": "custom", "name": "My Tool", "version": "1", "baseline": "ubuntu-base.qcow2", **extra}


def make(**extra):
    return pb.make_job(form(**extra), pb.load_recipes(), ["ubuntu-base.qcow2"], "admin")


def test_recipes_load_and_the_job_form_is_validated():
    recipes = pb.load_recipes()
    assert {"custom", "dvwa", "webgoat", "splunk"} <= set(recipes)
    assert recipes["webgoat"]["icon"] is not None and recipes["dvwa"]["script"]
    job = make(packages="nginx, git\nvim", cpus="4")
    assert job["packages"] == ["nginx", "git", "vim"] and job["cpus"] == 4 and job["image"] == "my-tool-1.qcow2"
    for bad in ({"name": "bad;name"}, {"name": ""}, {"baseline": "../../etc/passwd"}, {"recipe": "nope"}, {"packages": "ok; rm -rf /"},
                {"login_user": "Root User"}, {"cpus": "99"}, {"ram": "abc"}, {"custom_script": "x" * 70000}):
        with pytest.raises(ValueError):
            make(**bad)


def test_required_inputs_and_licence_checkbox_are_enforced():
    with pytest.raises(ValueError):
        make(recipe="splunk")
    full = {"input_SPLUNK_URL": "https://x/s.deb", "input_SPLUNK_SHA256": "a" * 64, "input_SPLUNK_ADMIN_PASSWORD": "longpassword"}
    with pytest.raises(ValueError, match="licence"):
        make(recipe="splunk", **full)
    job = make(recipe="splunk", input_SPLUNK_ACCEPT_LICENSE="yes", **full)
    assert job["inputs"]["SPLUNK_ACCEPT_LICENSE"] == "yes"
    with pytest.raises(ValueError):
        make(recipe="splunk", input_SPLUNK_ACCEPT_LICENSE="yes", **{**full, "input_SPLUNK_URL": "https://x\ninjected"})


def test_job_store_paths_logs_and_icons(tmp_path):
    store = pb.JobStore(tmp_path)
    for bad in ("../x", "abc", "0" * 31 + "/"):
        with pytest.raises(KeyError):
            store.path(bad)
    with pytest.raises(ValueError):
        store.create(make(), b"not a png")
    job_id = store.create(make(), PNG)
    assert (store.path(job_id) / "queued").exists() and (store.path(job_id) / "icon.png").read_bytes() == PNG
    assert store.get(job_id)["state"] == "queued"
    (store.path(job_id) / "console.log").write_bytes(b"line one\nline two\n")
    text, offset = store.read_log(job_id, 0)
    assert text == "line one\nline two\n" and store.read_log(job_id, offset) == ("", offset)
    store.cancel(job_id)
    assert (store.path(job_id) / "cancel").exists()


def test_descriptor_is_accepted_by_the_existing_appliance_publisher():
    job = make(notes="Login labadmin")
    template, images, notes = portal.appliance_template(pb.descriptor(job, pb.load_recipes()["custom"], job["image"], "d" * 32, 1234), "")
    assert template["name"].startswith("My Tool") and template["hda_disk_image"] == "my-tool-1.qcow2"
    assert images["my-tool-1.qcow2"]["md5"] == "d" * 32 and "Login labadmin" in notes


# --- the portal pages --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def client():
    with TestClient(portal.app) as test_client:
        test_client.post("/login", data={"username": "portaladmin", "password": "initial-admin-password"})
        test_client.post("/admin/users", data={"username": "plain2", "password": "plain2-password-1", "roles": ["lab_editor"]})
        yield test_client


def test_only_admins_reach_the_builder(client, base):
    assert client.get("/admin/appliance-builder").status_code == 200
    assert "ubuntu-base.qcow2" in client.get("/admin/appliance-builder").text
    other = TestClient(portal.app)
    other.post("/login", data={"username": "plain2", "password": "plain2-password-1"})
    assert other.get("/admin/appliance-builder").status_code == 403
    assert other.post("/admin/appliance-builder", data=form()).status_code == 403


def test_start_watch_cancel_and_publish_a_build(client, base):
    response = client.post("/admin/appliance-builder", data=form(name="Web Lab", version="2", notes="hello"), follow_redirects=False)
    assert response.status_code == 303
    job_id = response.headers["location"].rsplit("/", 1)[1]
    assert "queued" in client.get(f"/admin/appliance-builder/{job_id}").text
    # a second request for the same name and version is refused while the first is waiting
    assert "already+waiting" in client.post("/admin/appliance-builder", data=form(name="Web Lab", version="2"), follow_redirects=False).headers["location"]
    folder = pb.JobStore(base).path(job_id)
    (folder / "console.log").write_text("hello console\n")
    pb.write_json(folder / "status.json", {"state": "running", "message": "Installing", "percent": 40})
    data = client.get(f"/admin/appliance-builder/{job_id}/log?offset=0").json()
    assert data["text"] == "hello console\n" and data["state"] == "running" and data["percent"] == 40
    assert client.post(f"/admin/appliance-builder/{job_id}/publish", follow_redirects=False).headers["location"].endswith("not+finished+successfully.")
    client.post(f"/admin/appliance-builder/{job_id}/cancel")
    assert (folder / "cancel").exists()
    for bad in ("zz", "0" * 32):
        assert client.get(f"/admin/appliance-builder/{bad}").status_code == 404

    # a finished build goes through the normal review page: the disk is already in the store
    job = json.loads((folder / "job.json").read_text())
    (base / "images" / job["image"]).write_bytes(b"x" * 100)
    md5 = hashlib.md5(b"x" * 100).hexdigest()
    pb.write_json(folder / "result.gns3a", pb.descriptor(job, pb.load_recipes()["custom"], job["image"], md5, 100))
    pb.write_json(folder / "status.json", {"state": "succeeded", "message": "done", "percent": 100})
    review = client.post(f"/admin/appliance-builder/{job_id}/publish", follow_redirects=False)
    assert review.status_code == 303 and review.headers["location"].startswith("/admin/appliances/")
    page = client.get(review.headers["location"])
    assert page.status_code == 200 and "Web Lab" in page.text and "In the store" in page.text
    draft = next(iter(portal.app.state.appliance_drafts.values()))
    assert draft["icon"] == "" or draft["icon"].endswith("icon.png")


def test_an_existing_disk_in_the_store_is_never_replaced(client, base):
    (base / "images" / "my-tool-1.qcow2").write_bytes(b"existing")
    response = client.post("/admin/appliance-builder", data=form(), follow_redirects=False)
    assert "already+exists" in response.headers["location"]
    assert (base / "images" / "my-tool-1.qcow2").read_bytes() == b"existing"


def test_recipe_icon_is_attached_when_none_is_uploaded(client, base):
    response = client.post("/admin/appliance-builder", data=form(recipe="webgoat", name="Goat"), follow_redirects=False)
    folder = pb.JobStore(base).path(response.headers["location"].rsplit("/", 1)[1])
    assert (folder / "icon.png").read_bytes().startswith(pb.PNG_MAGIC)


# --- the builder service, run for real against stub tools ---------------------------------------

STUBS = {
    "qemu-img": 'case "$1" in convert) for last; do :; done; src=""; for a in "$@"; do src="$prev"; prev="$a"; done; cp "$src" "$last";; esac\nexit 0\n',
    "ssh-keygen": 'for a in "$@"; do last="$a"; done; echo KEY > "$last"; echo "ssh-ed25519 AAAA gns3-lab-builder" > "$last.pub"\n',
    "xorriso": 'for a in "$@"; do if [ "$prev" = "-o" ]; then touch "$a"; fi; prev="$a"; done\n',
    "qemu-system-x86_64": 'while [ ! -f "$STUB_OFF" ]; do sleep 0.1; done\n',
    "ssh": 'body=$(cat); echo "ssh $*" | head -c 60 >/dev/null\nif echo "$body" | grep -q gns3-firstboot; then echo GNS3-SEAL-COMPLETE; touch "$STUB_OFF"; else echo "$body" | grep -c "apt-get" >/dev/null; echo "ran: $(echo "$body" | grep -c .) lines"; fi\nexit 0\n',
}


@pytest.fixture
def service(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in STUBS.items():
        (bin_dir / name).write_text("#!/usr/bin/env bash\n" + body)
        (bin_dir / name).chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("STUB_OFF", str(tmp_path / "vm-off"))
    monkeypatch.setenv("PORTAL_BUILDER_DIR", str(tmp_path / "b"))
    monkeypatch.setenv("GNS3_IMAGES_DIR", str(tmp_path / "images"))
    (tmp_path / "images").mkdir()
    (tmp_path / "b" / "baselines").mkdir(parents=True)
    (tmp_path / "b" / "baselines" / "ubuntu-base.qcow2").write_bytes(b"BASELINE")
    spec = importlib.util.spec_from_file_location("appliance_builder_service", pb.ROOT / "scripts" / "appliance-builder.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["appliance_builder_service"] = module
    spec.loader.exec_module(module)
    return module, tmp_path


def test_service_builds_publishes_and_cleans_up(service):
    module, tmp = service
    store = pb.JobStore(tmp / "b")
    job_id = store.create(make(recipe="custom", packages="nginx", custom_script="echo hi"), PNG)
    module.Build(store.path(job_id)).execute()
    status = store.status(job_id)
    log = (store.path(job_id) / "console.log").read_text()
    assert status["state"] == "succeeded", (status, log)
    disk = tmp / "images" / "my-tool-1.qcow2"
    assert disk.read_bytes() == b"BASELINE"
    descriptor = json.loads((store.path(job_id) / "result.gns3a").read_text())
    assert descriptor["images"][0]["md5sum"] == hashlib.md5(b"BASELINE").hexdigest() and descriptor["images"][0]["filesize"] == 8
    assert "GNS3-SEAL-COMPLETE" in log and "== Installing the application" in log
    assert not (tmp / "b" / "work" / job_id).exists() and not (store.path(job_id) / "queued").exists()   # key and payload removed
    assert not list((tmp / "images").glob("*.partial"))


def test_service_refuses_to_replace_a_disk_and_reports_failures(service):
    module, tmp = service
    store = pb.JobStore(tmp / "b")
    (tmp / "images" / "my-tool-1.qcow2").write_bytes(b"keep me")
    job_id = store.create(make())
    module.Build(store.path(job_id)).execute()
    assert store.status(job_id)["state"] == "failed" and "already exists" in store.status(job_id)["message"]
    assert (tmp / "images" / "my-tool-1.qcow2").read_bytes() == b"keep me"
    (tmp / "images" / "my-tool-1.qcow2").unlink()
    (tmp / "b" / "baselines" / "ubuntu-base.qcow2").unlink()   # baseline vanished: the clone step fails, nothing is published
    second = store.create(make())
    module.Build(store.path(second)).execute()
    assert store.status(second)["state"] == "failed" and not list((tmp / "images").iterdir())


def test_service_payload_quotes_inputs_and_cancel_stops_a_build(service):
    module, tmp = service
    store = pb.JobStore(tmp / "b")
    job_id = store.create(make(recipe="splunk", input_SPLUNK_URL="https://x/$(touch pwned)", input_SPLUNK_SHA256="a" * 64,
                               input_SPLUNK_ADMIN_PASSWORD="pass word'; rm", input_SPLUNK_ACCEPT_LICENSE="yes"))
    build = module.Build(store.path(job_id))
    build.work.mkdir(parents=True)
    script = build.payload(pb.load_recipes()["splunk"]).read_text()
    assert "export SPLUNK_URL='https://x/$(touch pwned)'" in script and "pass word'\"'\"'; rm" in script
    assert stat.S_IMODE((build.work / "payload.sh").stat().st_mode) == 0o600
    cancelled = store.create(make(name="Other"))
    (store.path(cancelled) / "cancel").touch()
    module.Build(store.path(cancelled)).execute()
    assert store.status(cancelled)["state"] == "cancelled" and not (tmp / "images" / "other-1.qcow2").exists()
