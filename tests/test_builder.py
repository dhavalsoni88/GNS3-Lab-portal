"""Appliance builder: job validation, the portal pages, and the builder service run against stub tools."""
import hashlib
import importlib.util
import io
import json
import os
import stat
import sys
import zipfile

import pytest
from fastapi.testclient import TestClient

import app as portal
import portal_builder as pb
import portal_db as pb_db

PNG = pb.PNG_MAGIC + b"fake"


@pytest.fixture
def base(tmp_path, monkeypatch):
    (tmp_path / "baselines").mkdir()
    (tmp_path / "baselines" / "ubuntu-base.qcow2").write_bytes(b"BASELINE-DISK")
    (tmp_path / "images").mkdir()
    monkeypatch.setattr(portal, "BUILDER_DIR", tmp_path)
    monkeypatch.setattr(portal, "GNS3_IMAGES_DIR", tmp_path / "images")
    return tmp_path


PASSWORD = "baseline pass 'quoted' $x"


def create(store, job, icon=None):
    return store.create(job, icon, password=PASSWORD)


def form(**extra):
    return {"recipe": "custom", "name": "My Tool", "version": "1", "baseline": "ubuntu-base.qcow2", "seal_script": "true", "login_password": PASSWORD, **extra}


def make(**extra):
    return pb.make_job(form(**extra), pb.load_recipes(), ["ubuntu-base.qcow2"], "admin")


def test_recipes_load_and_the_job_form_is_validated():
    recipes = pb.load_recipes()
    assert {"custom", "dvwa", "webgoat", "splunk"} <= set(recipes)
    assert recipes["webgoat"]["icon"] is not None and recipes["dvwa"]["hooks"]["install-app.sh"]
    job = make(packages="nginx, git\nvim", cpus="4")
    assert job["packages"] == ["nginx", "git", "vim"] and job["cpus"] == 4 and job["image"] == "my-tool-1.qcow2"
    for bad in ({"name": "bad;name"}, {"name": ""}, {"baseline": "../../etc/passwd"}, {"recipe": "nope"}, {"packages": "ok; rm -rf /"},
                {"login_user": "Root User"}, {"cpus": "99"}, {"ram": "abc"}, {"custom_script": "x" * 70000}):
        with pytest.raises(ValueError):
            make(**bad)


def test_requirements_licence_and_custom_cleanup_are_enforced():
    splunk = pb.load_recipes()["splunk"]
    assert splunk["min_ram"] == 12288 and splunk["cpu_flags"] == ["avx", "sse4_2", "aes"] and splunk["requires_license"]
    assert "initialize-splunk-lab" in splunk["hooks"]["install-app.sh"] and "PASSWORD" not in json.dumps({k: v for k, v in splunk.items() if k not in ("hooks", "icon")})
    with pytest.raises(ValueError, match="licence"):
        make(recipe="splunk")
    with pytest.raises(ValueError, match="Memory"):
        make(recipe="splunk", accept_license="yes", ram="8192")   # below the application's minimum
    with pytest.raises(ValueError, match="CPUs"):
        make(recipe="webgoat", cpus="1")
    job = make(recipe="splunk", accept_license="yes")
    assert job["license_accepted"] and job["ram"] == 12288 and job["recipe_snapshot"]["hooks"]["seal-app.sh"] == splunk["hooks"]["seal-app.sh"]
    with pytest.raises(ValueError, match="clean-up script"):
        make(seal_script="  ")
    custom = make(check_script="curl -fs localhost", seal_script="rm -f /etc/app-id")
    assert custom["recipe_snapshot"]["hooks"] == {"install-app.sh": "", "check-app.sh": "curl -fs localhost", "seal-app.sh": "rm -f /etc/app-id"}


def test_recipe_settings_accept_generator_profiles_and_refuse_bad_values():
    profile = {"id": "elk", "display_name": "Elastic", "version": "9", "cpus": 4, "ram": 8192, "min_cpus": 2, "min_ram": 6144, "disk_gib": 40,
               "min_free_gib": 7.5, "cpu_flags": ["avx"], "requires_license": False, "services": ["elasticsearch.service"],
               "ports": [{"name": "web", "host_port": 15601, "guest_port": 5601}], "usage": "Kibana on 5601"}
    settings = pb.recipe_settings(profile)
    assert settings["label"] == "Elastic" and settings["disk_gb"] == 40 and settings["min_free_gib"] == 8 and settings["ports"] == [{"name": "web", "guest_port": 5601}]
    for bad in ({"min_ram": 9000}, {"cpu_flags": ["avx; rm"]}, {"services": ["a b"]}, {"cpus": 99}, {"requires_license": "yes"}, {"license_url": "http://x"}):
        with pytest.raises(ValueError):
            pb.recipe_settings({"label": "X", "ram": 8192, **bad})


def test_job_store_paths_logs_and_icons(tmp_path):
    store = pb.JobStore(tmp_path)
    for bad in ("../x", "abc", "0" * 31 + "/"):
        with pytest.raises(KeyError):
            store.path(bad)
    with pytest.raises(ValueError):
        create(store, make(), b"not a png")
    job_id = create(store, make(), PNG)
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
    assert "secret" in [p.name for p in pb.JobStore(base).path(job_id).iterdir()] and PASSWORD not in (pb.JobStore(base).path(job_id) / "job.json").read_text()
    assert "password" in client.post("/admin/appliance-builder", data=form(name="No Pw", login_password=""), follow_redirects=False).headers["location"]
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
    "qemu-img": 'echo "$*" >> "$STUB_QEMU_LOG"\ncase "$1" in info) echo "{\\"virtual-size\\": $(( ${STUB_DISK_GIB:-8} * 1073741824 ))}";; convert) for last; do :; done; src=""; for a in "$@"; do src="$prev"; prev="$a"; done; cp "$src" "$last";; esac\nexit 0\n',
    "ssh-keygen": 'for a in "$@"; do last="$a"; done; echo KEY > "$last"; echo "ssh-ed25519 AAAA gns3-lab-builder" > "$last.pub"\n',
    "xorriso": 'for a in "$@"; do if [ "$prev" = "-o" ]; then touch "$a"; fi; prev="$a"; done\n',
    "qemu-system-x86_64": 'while [ ! -f "$STUB_OFF" ]; do sleep 0.1; done\n',
    "ssh": 'case "$*" in *"read -r p"*) IFS= read -r given; [ "$given" = "$STUB_PASSWORD" ] && [ "$SSH_ASKPASS_REQUIRE" = force ] && [ "$($SSH_ASKPASS)" = "$STUB_PASSWORD" ] && echo "Build access added." && exit 0; echo "Permission denied"; exit 255;; esac\nbody=$(cat); if [ -n "$body" ]; then n=$(ls "$STUB_STAGES" | wc -l); printf "%s" "$body" > "$STUB_STAGES/$n.sh"; fi\nif echo "$body" | grep -q gns3-firstboot; then echo GNS3-SEAL-COMPLETE; touch "$STUB_OFF"; fi\nif [ -n "$STUB_FAIL" ] && echo "$body" | grep -q "$STUB_FAIL"; then echo "stub failure"; exit 3; fi\nexit 0\n',
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
    monkeypatch.setenv("STUB_STAGES", str(tmp_path / "stages"))
    monkeypatch.setenv("STUB_FAIL", "")
    monkeypatch.setenv("STUB_PASSWORD", PASSWORD)
    monkeypatch.setenv("STUB_QEMU_LOG", str(tmp_path / "qemu-img.log"))
    (tmp_path / "stages").mkdir()
    monkeypatch.setenv("PORTAL_BUILDER_DIR", str(tmp_path / "b"))
    monkeypatch.setenv("GNS3_IMAGES_DIR", str(tmp_path / "images"))
    (tmp_path / "images").mkdir()
    (tmp_path / "b" / "baselines").mkdir(parents=True)
    (tmp_path / "b" / "baselines" / "ubuntu-base.qcow2").write_bytes(b"BASELINE")
    spec = importlib.util.spec_from_file_location("appliance_builder_service", pb.ROOT / "scripts" / "appliance-builder.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["appliance_builder_service"] = module
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "host_facts", lambda: (64, 65536, {"avx", "sse4_2", "aes"}))
    monkeypatch.setattr(module, "ssh_banner", lambda port: True)
    return module, tmp_path


def stages(tmp):
    return [(tmp / "stages" / f"{n}.sh").read_text() for n in range(len(list((tmp / "stages").iterdir())))]


def test_service_builds_publishes_and_cleans_up(service):
    module, tmp = service
    store = pb.JobStore(tmp / "b")
    job_id = create(store, make(recipe="custom", packages="nginx", custom_script="echo hi"), PNG)
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
    job_id = create(store, make())
    module.Build(store.path(job_id)).execute()
    assert store.status(job_id)["state"] == "failed" and "already exists" in store.status(job_id)["message"]
    assert (tmp / "images" / "my-tool-1.qcow2").read_bytes() == b"keep me"
    (tmp / "images" / "my-tool-1.qcow2").unlink()
    (tmp / "b" / "baselines" / "ubuntu-base.qcow2").unlink()   # baseline vanished: the clone step fails, nothing is published
    second = create(store, make())
    module.Build(store.path(second)).execute()
    assert store.status(second)["state"] == "failed" and not list((tmp / "images").iterdir())


def test_service_runs_install_check_and_seal_in_order(service):
    module, tmp = service
    store = pb.JobStore(tmp / "b")
    job_id = create(store, make(recipe="dvwa", name="DVWA", packages="vim", custom_script="echo extra"))
    module.Build(store.path(job_id)).execute()
    assert store.status(job_id)["state"] == "succeeded", (tmp / "b" / "jobs" / job_id / "console.log").read_text()
    install, check, seal = [text for text in stages(tmp) if text.strip()]   # the SSH probe sends nothing
    assert install.index("apt-get install -y") < install.index("== install-app.sh") < install.index("== your-script.sh")
    assert "== check-app.sh" in check and "seal-app.sh" not in check
    order = [seal.index(marker) for marker in ("== check-app.sh", "systemctl stop apache2", "systemctl stop mariadb", "== seal-app.sh", "rm -rf /root/.gns3-build", "gns3-firstboot")]
    assert order == sorted(order)
    assert "APPLIANCE_ACCEPT_LICENSE" not in install   # only a recipe that needs a licence, and only after acceptance


def test_a_failing_check_stops_the_build_before_sealing(service, monkeypatch):
    module, tmp = service
    monkeypatch.setenv("STUB_FAIL", "PASS: DVWA answers")   # text that only the check stage contains
    store = pb.JobStore(tmp / "b")
    job_id = create(store, make(recipe="dvwa", name="DVWA"))
    module.Build(store.path(job_id)).execute()
    assert store.status(job_id)["state"] == "failed" and "check failed" in store.status(job_id)["message"]
    assert not any("gns3-firstboot" in text for text in stages(tmp)) and not list((tmp / "images").iterdir())


def test_host_checks_refuse_a_build_the_server_cannot_hold(service, monkeypatch):
    module, tmp = service
    store = pb.JobStore(tmp / "b")
    for facts, expected in (((2, 65536, {"avx", "sse4_2", "aes"}), "needs 4 CPUs"), ((64, 4096, {"avx", "sse4_2", "aes"}), "only 4096 MiB"), ((64, 65536, {"sse4_2"}), "avx, aes")):
        monkeypatch.setattr(module, "host_facts", lambda facts=facts: facts)
        job_id = create(store, make(recipe="splunk", accept_license="yes", name=f"S {len(expected)}"))
        module.Build(store.path(job_id)).execute()
        assert store.status(job_id)["state"] == "failed" and expected in store.status(job_id)["message"], store.status(job_id)
    job = make(name="Huge")
    job["recipe_snapshot"]["min_free_gib"] = 10**6
    job_id = create(store, job)
    module.Build(store.path(job_id)).execute()
    assert "GiB free" in store.status(job_id)["message"] and not stages(tmp)   # nothing was copied or booted


def test_stage_scripts_keep_hooks_apart_and_pass_the_licence(service):
    module, tmp = service
    store = pb.JobStore(tmp / "b")
    job_id = create(store, make(recipe="splunk", accept_license="yes", name="Splunk"))
    build = module.Build(store.path(job_id))
    build.work.mkdir(parents=True)
    recipe = build.job["recipe_snapshot"]
    recipe["hooks"]["install-app.sh"] = "echo '$(touch pwned)'\nexit 0"
    text = build.install_stage(recipe).read_text()
    assert "export APPLIANCE_ACCEPT_LICENSE=1" in text
    marker = text.split("cat > /root/.gns3-build/install-app.sh <<'", 1)[1].split("'", 1)[0]
    assert marker.startswith("GNS3_HOOK_") and text.count(marker) == 2 and "bash /root/.gns3-build/install-app.sh" in text   # exit 0 ends only the hook
    assert stat.S_IMODE((build.work / "install.sh").stat().st_mode) == 0o600
    cancelled = create(store, make(name="Other"))
    (store.path(cancelled) / "cancel").touch()
    module.Build(store.path(cancelled)).execute()
    assert store.status(cancelled)["state"] == "cancelled" and not (tmp / "images" / "other-1.qcow2").exists()


# --- importing applications -----------------------------------------------------------------------

def recipe_zip(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


GENERATOR = {   # the layout of ubuntu-appliance-generator: profiles/, hooks/<id>/, assets/
    "gen/profiles/elk.json": json.dumps({"id": "elk", "display_name": "Elastic", "version": "9", "cpus": 4, "ram": 8192, "min_ram": 6144, "disk_gib": 40,
                                         "services": [], "hooks": {"install-app.sh": "../hooks/elk/install-app.sh", "check-app.sh": "../hooks/elk/check-app.sh",
                                                                   "seal-app.sh": "../hooks/elk/seal-app.sh"}, "icon": "../assets/elk.png"}),
    "gen/hooks/elk/install-app.sh": "apt-get install -y elasticsearch\n", "gen/hooks/elk/check-app.sh": "true\n", "gen/hooks/elk/seal-app.sh": "rm -f /etc/elk-id\n",
    "gen/assets/elk.png": PNG, "gen/generate.py": "print('not used')\n",
}


def test_import_generator_profile_and_plain_recipe(tmp_path):
    folder = tmp_path / "recipes"
    assert pb.import_recipe(recipe_zip(GENERATOR), folder, set(pb.load_recipes(folder))) == "elk"
    elk = pb.load_recipes(folder)["elk"]
    assert elk["source"] == "imported" and elk["min_ram"] == 6144 and elk["hooks"]["seal-app.sh"] == "rm -f /etc/elk-id\n" and elk["icon"].read_bytes() == PNG
    plain = {"recipe.json": json.dumps({"label": "Nginx test", "packages": ["nginx"]}), "install-app.sh": "true", "check-app.sh": "true", "seal-app.sh": "true"}
    assert pb.import_recipe(recipe_zip(plain), folder, set(pb.load_recipes(folder))) == "nginx-test"
    for files, message in ((GENERATOR, "already exists"),
                           ({**plain, "recipe.json": json.dumps({"id": "dvwa", "label": "Mine"})}, "already exists"),   # built-in ids cannot be replaced
                           ({k: v for k, v in plain.items() if k != "seal-app.sh"} | {"recipe.json": json.dumps({"id": "x1", "label": "X"})}, "seal-app.sh"),
                           ({"p/app.json": json.dumps({"id": "x2", "display_name": "X", "hooks": {"install-app.sh": "../../etc/passwd", "check-app.sh": "c", "seal-app.sh": "s"}})}, "not in the zip"),
                           ({"a.json": "{}", "b.json": "{}"}, "exactly one")):
        with pytest.raises(ValueError, match=message):
            pb.import_recipe(recipe_zip(files), folder, set(pb.load_recipes(folder)))
    with pytest.raises(ValueError, match="not a zip"):
        pb.import_recipe(b"plain text", folder, set())
    assert sorted(p.name for p in folder.iterdir()) == ["elk", "nginx-test"]   # nothing half-written is left behind
    pb.delete_recipe(folder, "elk")
    assert "elk" not in pb.load_recipes(folder)


def test_import_view_and_delete_in_the_portal(client, base):
    response = client.post("/admin/appliance-builder/recipes", files={"recipe_file": ("elk.zip", recipe_zip(GENERATOR), "application/zip")}, follow_redirects=False)
    assert response.headers["location"].startswith("/admin/appliance-builder/recipes/elk")
    page = client.get("/admin/appliance-builder/recipes/elk")
    assert page.status_code == 200 and "rm -f /etc/elk-id" in page.text and "6144" in page.text
    assert "Elastic" in client.get("/admin/appliance-builder").text
    assert client.post("/admin/appliance-builder/recipes/dvwa/delete").status_code == 404   # built-in recipes stay
    other = TestClient(portal.app)
    other.post("/login", data={"username": "plain2", "password": "plain2-password-1"})
    assert other.post("/admin/appliance-builder/recipes", files={"recipe_file": ("x.zip", recipe_zip(GENERATOR), "application/zip")}).status_code == 403
    assert client.post("/admin/appliance-builder/recipes/elk/delete", follow_redirects=False).status_code == 303
    assert client.get("/admin/appliance-builder/recipes/elk").status_code == 404
    with pb_db.connect() as connection:
        actions = {row["action"] for row in connection.execute("SELECT action FROM audit_log WHERE target='elk'").fetchall()}
    assert {"builder_recipe_imported", "builder_recipe_deleted"} <= actions


def test_the_disk_is_grown_but_never_shrunk(service, monkeypatch):
    module, tmp = service
    store = pb.JobStore(tmp / "b")
    monkeypatch.setenv("STUB_DISK_GIB", "32")   # like the ironman baseline: 32 GiB, more than DVWA's 16
    job_id = create(store, make(recipe="dvwa", name="Small"))
    module.Build(store.path(job_id)).execute()
    assert store.status(job_id)["state"] == "succeeded", (store.path(job_id) / "console.log").read_text()
    assert "resize" not in (tmp / "qemu-img.log").read_text() and "Disk kept at 32 GiB" in (store.path(job_id) / "console.log").read_text()
    monkeypatch.setenv("STUB_DISK_GIB", "8")
    grown = create(store, make(recipe="dvwa", name="Grown", disk_gb="20"))
    module.Build(store.path(grown)).execute()
    assert store.status(grown)["state"] == "succeeded" and any(line.startswith("resize") and line.endswith(" 20G") for line in (tmp / "qemu-img.log").read_text().splitlines())


def test_password_is_used_once_and_never_kept(service, monkeypatch):
    module, tmp = service
    store = pb.JobStore(tmp / "b")
    job_id = create(store, make(name="Pw"))
    folder = store.path(job_id)
    assert oct((folder / "secret").stat().st_mode & 0o777) == "0o640" and PASSWORD not in (folder / "job.json").read_text()
    module.Build(folder).execute()
    assert store.status(job_id)["state"] == "succeeded", (folder / "console.log").read_text()
    assert not (folder / "secret").exists() and PASSWORD not in (folder / "console.log").read_text() and "Build access added" in (folder / "console.log").read_text()
    assert not any(PASSWORD in text for text in stages(tmp))
    seal = [t for t in stages(tmp) if "gns3-firstboot" in t][0]
    assert "/etc/sudoers.d/90-gns3-builder" in seal   # the temporary sudo rule goes before the image is published
    monkeypatch.setenv("STUB_PASSWORD", "something else")
    wrong = create(store, make(name="Wrong"))
    module.Build(store.path(wrong)).execute()
    assert store.status(wrong)["state"] == "failed" and "password" in store.status(wrong)["message"] and not list((tmp / "images").glob("wrong*"))
    none = store.create(make(name="None"))
    module.Build(store.path(none)).execute()
    assert "No password" in store.status(none)["message"]
