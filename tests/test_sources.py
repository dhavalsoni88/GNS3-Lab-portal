"""From a pasted link or package name to a draft application: classification, drafts per source, the analysis job and the review pages."""
import json
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

import app as portal
import builder_sources as sources
import portal_builder as pb
from test_builder import client, service  # noqa: F401  (shared fixtures)

SPLUNK = "https://download.splunk.com/products/splunk/releases/10.6.2.5/linux/splunk-10.6.2.5-b07ce3b48d58-linux-amd64.deb"


def test_classify_links_commands_and_package_names():
    assert sources.classify(f'wget -O splunk.deb "{SPLUNK}"') == ("deb", SPLUNK)
    for link in ("https://github.com/digininja/DVWA", "https://github.com/digininja/DVWA.git", "https://www.github.com/digininja/DVWA/tree/master/docs"):
        assert sources.classify(link) == ("github", "digininja/DVWA")
    assert sources.classify("nginx, postgresql-16") == ("apt", "nginx postgresql-16")
    for bad, message in (("http://example.com/a.deb", "https"), ("https://10.0.0.5/a.deb", "public"), ("https://localhost/a.deb", "public"),
                         ("https://example.com/app.tar.gz", ".deb"), ("https://github.com/only-owner", "repository's own link"), ("rm -rf /", "Paste")):
        with pytest.raises(ValueError, match=message):
            sources.classify(bad)


def bash_ok(hooks):
    for name, text in hooks.items():
        result = subprocess.run(["bash", "-n"], input=text, text=True, capture_output=True)
        assert result.returncode == 0, (name, result.stderr, text)


def make_deb(tmp_path, arch="amd64", service=True):
    root = tmp_path / "pkg"
    (root / "DEBIAN").mkdir(parents=True)
    (root / "DEBIAN" / "control").write_text(f"Package: demo-app\nVersion: 2.1\nArchitecture: {arch}\nMaintainer: x <x@example.invalid>\n"
                                             "Depends: libc6, python3\nInstalled-Size: 3145728\nHomepage: https://demo.example.com\nDescription: Demo service\n long text\n")
    if service:
        (root / "lib" / "systemd" / "system").mkdir(parents=True)
        (root / "lib" / "systemd" / "system" / "demo-app.service").write_text("[Service]\nExecStart=/bin/true\n")
    deb = tmp_path / "demo.deb"
    subprocess.run(["dpkg-deb", "--build", "--root-owner-group", str(root), str(deb)], check=True, capture_output=True)
    return deb


def fake_download(deb):
    def download(url, target, say):
        shutil.copy(deb, target)
        return "ab" * 32, deb.stat().st_size
    return download


def test_deb_draft_reads_the_package(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "download", fake_download(make_deb(tmp_path)))
    (tmp_path / "work").mkdir()
    draft = sources.draft(f"wget {SPLUNK}", tmp_path / "work", print, set(), {"splunk"})
    settings, hooks = draft["settings"], draft["hooks"]
    assert draft["id"] == "demo-app" and settings["services"] == ["demo-app.service"] and settings["ram"] == 4096   # 3 GiB installed
    assert settings["disk_gb"] >= 16 and settings["min_free_gib"] >= 10 and settings["requires_license"]
    assert f'app.deb" {SPLUNK}\n' in hooks["install-app.sh"] and "ab" * 32 in hooks["install-app.sh"] and "systemctl enable --now demo-app.service" in hooks["install-app.sh"]
    assert "libc6, python3" in hooks["install-app.sh"] and "demo-app.service" in hooks["check-app.sh"]
    bash_ok(hooks)
    odd = "https://example.com/$(touch${IFS}pwned)/a.deb"
    assert "'https://example.com/$(touch${IFS}pwned)/a.deb'" in sources.draft(odd, tmp_path / "work", print, {"demo-app"}, set())["hooks"]["install-app.sh"]
    monkeypatch.setattr(sources, "download", fake_download(make_deb(tmp_path / "arm", arch="arm64")))
    with pytest.raises(ValueError, match="arm64"):
        sources.draft(SPLUNK, tmp_path / "arm", print, set(), set())


def github(monkeypatch, files, release=None, licence="mit", raw=""):
    api = "https://api.github.com/repos/acme/vuln-app"
    answers = {api: {"full_name": "acme/vuln-app", "default_branch": "main", "description": "A vulnerable app", "size": 2048,
                     "html_url": "https://github.com/acme/vuln-app", "license": {"spdx_id": licence.upper()} if licence else None},
               f"{api}/commits/main": {"sha": "c0ffee" * 6 + "abcd"},
               f"{api}/contents?ref={'c0ffee' * 6 + 'abcd'}": [{"name": f, "type": "file"} for f in files]}

    def fetch_json(url):
        if url == f"{api}/releases/latest":
            if release is None:
                raise OSError("HTTP Error 404: Not Found")
            return release
        return answers[url]
    monkeypatch.setattr(sources, "fetch_json", fetch_json)
    monkeypatch.setattr(sources, "fetch_text", lambda url: raw)


def test_github_compose_dockerfile_installer_and_release(tmp_path, monkeypatch):
    github(monkeypatch, ["docker-compose.yml", "README.md"], raw='services:\n  web:\n    ports:\n      - "8080:80"\n      - 3306:3306\n')
    draft = sources.draft("https://github.com/acme/vuln-app", tmp_path, print, set(), set())
    s = draft["settings"]
    assert s["services"] == ["vuln-app-app.service"] and [p["guest_port"] for p in s["ports"]] == [3306, 8080] and "docker.io" in s["packages"]
    assert not s["requires_license"] and "checkout --quiet --detach c0ffee" in draft["hooks"]["install-app.sh"] and "docker compose -f docker-compose.yml up -d --build" in draft["hooks"]["install-app.sh"]
    bash_ok(draft["hooks"])

    github(monkeypatch, ["Dockerfile"], licence="", raw="FROM nginx\nEXPOSE 80/tcp 443\n")
    draft = sources.draft("https://github.com/acme/vuln-app", tmp_path, print, set(), set())
    assert "docker build -t gns3-lab/vuln-app:" in draft["hooks"]["install-app.sh"] and "-p 80:80 -p 443:443" in draft["hooks"]["install-app.sh"]
    assert draft["settings"]["requires_license"]   # no licence declared: the form asks
    bash_ok(draft["hooks"])

    github(monkeypatch, ["install.sh"])
    draft = sources.draft("https://github.com/acme/vuln-app", tmp_path, print, {"vuln-app"}, set())
    assert draft["id"] == "vuln-app-2" and "bash ./install.sh" in draft["hooks"]["install-app.sh"]
    bash_ok(draft["hooks"])

    monkeypatch.setattr(sources, "download", fake_download(make_deb(tmp_path / "rel")))
    github(monkeypatch, ["Dockerfile"], release={"tag_name": "v2", "assets": [{"name": "demo_2_arm64.deb", "browser_download_url": "https://x/a.deb"},
                                                                                {"name": "demo_2_amd64.deb", "browser_download_url": "https://x/b.deb"}]})
    draft = sources.draft("https://github.com/acme/vuln-app", tmp_path / "rel", print, set(), set())
    assert 'app.deb" https://x/b.deb\n' in draft["hooks"]["install-app.sh"] and not draft["settings"]["requires_license"]   # MIT repo

    github(monkeypatch, ["README.md", "main.go"])
    with pytest.raises(ValueError, match="Custom"):
        sources.draft("https://github.com/acme/vuln-app", tmp_path, print, set(), set())


def test_apt_draft(monkeypatch, tmp_path):
    monkeypatch.setattr(sources, "apt_facts", lambda names: {"nginx": {"Version": "1.26", "Installed-Size": "1500", "Description": "web server"}, "nosuch": {}})
    draft = sources.draft("nginx nosuch", tmp_path, print, set(), set())
    assert draft["settings"]["packages"] == ["nginx", "nosuch"] and any("nosuch" in note for note in draft["notes"])
    bash_ok(draft["hooks"])


def test_analysis_job_writes_a_draft(service, monkeypatch):
    module, tmp = service
    github(monkeypatch, ["compose.yaml"], raw="services: {}\n")
    store = pb.JobStore(tmp / "b")
    job_id = store.create(pb.make_analysis_job("https://github.com/acme/vuln-app", "admin"))
    module.Analysis(store.path(job_id)).execute()
    assert store.status(job_id)["state"] == "succeeded", (store.path(job_id) / "console.log").read_text()
    assert json.loads((store.path(job_id) / "draft.json").read_text())["id"] == "vuln-app"
    monkeypatch.setattr(sources, "fetch_json", lambda url: (_ for _ in ()).throw(OSError("network down")))
    failed = store.create(pb.make_analysis_job("https://github.com/acme/vuln-app", "admin"))
    module.Analysis(store.path(failed)).execute()
    assert store.status(failed)["state"] == "failed" and "network down" in store.status(failed)["message"]


def test_review_save_and_edit_in_the_portal(client, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setattr(portal, "BUILDER_DIR", tmp_path)
    response = client.post("/admin/appliance-builder/analyze", data={"source": "https://github.com/acme/vuln-app"}, follow_redirects=False)
    job_id = response.headers["location"].rsplit("/", 1)[1]
    assert "Analysing https://github.com/acme/vuln-app" in client.get(f"/admin/appliance-builder/{job_id}").text
    assert "error=" in client.post("/admin/appliance-builder/analyze", data={"source": "rm -rf /"}, follow_redirects=False).headers["location"]
    github(monkeypatch, ["compose.yaml"], raw="services: {}\n")
    draft = sources.draft("https://github.com/acme/vuln-app", tmp_path, print, set(), set())
    pb.write_json(pb.JobStore(tmp_path).path(job_id) / "draft.json", draft)
    page = client.get(f"/admin/appliance-builder/{job_id}/draft")
    assert page.status_code == 200 and "docker compose" in page.text and "What was found" in page.text
    form = {"id": "vuln-app", **{k: " ".join(map(str, v)) if isinstance(v, list) and k != "ports" else v for k, v in draft["settings"].items() if k != "ports"},
            "ports": "web:8080", **draft["hooks"]}
    form["requires_license"] = ""
    assert "Confirm" in client.post(f"/admin/appliance-builder/{job_id}/draft", data=form, follow_redirects=False).headers["location"]
    saved = client.post(f"/admin/appliance-builder/{job_id}/draft", data={**form, "reviewed": "yes"}, follow_redirects=False)
    assert saved.headers["location"].startswith("/admin/appliance-builder/recipes/vuln-app")
    recipe = pb.load_recipes(tmp_path / "recipes")["vuln-app"]
    assert recipe["source"] == "imported" and recipe["ports"] == [{"name": "web", "guest_port": 8080}] and "docker compose" in recipe["hooks"]["install-app.sh"]
    assert "already+exists" in client.post(f"/admin/appliance-builder/{job_id}/draft", data={**form, "reviewed": "yes"}, follow_redirects=False).headers["location"]
    assert client.get("/admin/appliance-builder/recipes/vuln-app/edit").status_code == 200
    client.post("/admin/appliance-builder/recipes/vuln-app/edit", data={**form, "ram": "6144", "reviewed": "yes"})
    assert pb.load_recipes(tmp_path / "recipes")["vuln-app"]["ram"] == 6144
    assert "cannot+be+empty" in client.post("/admin/appliance-builder/recipes/vuln-app/edit", data={**form, "seal-app.sh": " ", "reviewed": "yes"}, follow_redirects=False).headers["location"]
    assert client.get("/admin/appliance-builder/recipes/dvwa/edit").status_code == 404   # built-in recipes are edited in the repository
    other = TestClient(portal.app)
    other.post("/login", data={"username": "plain2", "password": "plain2-password-1"})
    assert other.post("/admin/appliance-builder/analyze", data={"source": "nginx"}).status_code == 403
