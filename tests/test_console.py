"""Manual builds and the VM screen: Windows and Linux by hand in the browser, ISOs, and the screen relay."""
import asyncio
import json
import socket
import threading
import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import app as portal
import portal_builder as pb
from test_builder import PASSWORD, base, client, create, form, make, service  # noqa: F401  (shared fixtures)

ORIGIN = {"origin": "http://testserver"}


def run_in_background(build):
    thread = threading.Thread(target=build.execute, daemon=True)
    thread.start()
    return thread


def wait_for(predicate, seconds=20):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def test_manual_recipes_iso_rules_and_windows_appliance():
    recipes = pb.load_recipes()
    assert recipes["manual-windows"]["interactive"] and recipes["manual-windows"]["os"] == "windows" and not recipes["manual-windows"]["hooks"]
    job = pb.make_job(form(recipe="manual-windows", baseline="iso:win2022.iso"), recipes, [], "admin", ["win2022.iso"])
    assert job["iso"] == "win2022.iso" and job["baseline"] == "" and job["disk_gb"] == 64
    with pytest.raises(ValueError, match="by hand"):
        pb.make_job(form(recipe="dvwa", baseline="iso:win2022.iso"), recipes, [], "admin", ["win2022.iso"])
    with pytest.raises(ValueError, match="ISOs listed"):
        pb.make_job(form(recipe="manual-windows", baseline="iso:../../etc/shadow"), recipes, [], "admin", ["win2022.iso"])
    descriptor = pb.descriptor(job, recipes["manual-windows"], "w.qcow2", "a" * 32, 10)
    assert descriptor["qemu"]["adapter_type"] == "e1000" and descriptor["qemu"]["hda_disk_interface"] == "sata"
    template, _, notes = portal.appliance_template(descriptor, "")   # the Windows options pass the QEMU option allowlist
    assert "-rtc base=localtime" in template["options"] and "Installed from win2022.iso" in notes


def test_manual_windows_from_an_iso_publishes_when_windows_shuts_down(service):
    module, tmp = service
    (tmp / "b" / "isos").mkdir()
    (tmp / "b" / "isos" / "win2022.iso").write_bytes(b"ISO")
    store = pb.JobStore(tmp / "b")
    job = pb.make_job(form(recipe="manual-windows", name="Win", baseline="iso:win2022.iso"), pb.load_recipes(), [], "admin", ["win2022.iso"])
    job_id = store.create(job)   # no password: Windows never needs one
    thread = run_in_background(module.Build(store.path(job_id)))
    assert wait_for(lambda: store.status(job_id)["vnc_port"]), (store.path(job_id) / "console.log").read_text()
    assert "sysprep.exe /generalize /oobe /shutdown" in store.status(job_id)["message"]
    vm = (tmp / "qemu-img.log.vm").read_text()
    assert "-machine q35" in vm and "e1000,netdev=n0" in vm and "ide-cd,drive=cd0" in vm and "win2022.iso" in vm and "-vnc 127.0.0.1:" in vm and "hostfwd" not in vm
    assert any(line.startswith("create -q -f qcow2") and line.endswith(" 64G") for line in (tmp / "qemu-img.log").read_text().splitlines())
    (tmp / "vm-off").touch()   # Sysprep shut Windows down
    thread.join(30)
    assert store.status(job_id)["state"] == "succeeded", (store.status(job_id), (store.path(job_id) / "console.log").read_text()[-800:])
    assert store.status(job_id)["vnc_port"] == 0
    assert json.loads((store.path(job_id) / "result.gns3a").read_text())["qemu"]["adapter_type"] == "e1000"


def test_manual_linux_seal_and_finish_and_shutting_down_yourself(service):
    module, tmp = service
    store = pb.JobStore(tmp / "b")
    job_id = create(store, pb.make_job(form(recipe="manual-linux", name="Desk"), pb.load_recipes(), ["ubuntu-base.qcow2"], "admin"))
    thread = run_in_background(module.Build(store.path(job_id)))
    assert wait_for(lambda: "Seal and finish" in store.status(job_id)["message"])
    (store.path(job_id) / "finish").touch()
    thread.join(60)
    assert store.status(job_id)["state"] == "succeeded", (store.path(job_id) / "console.log").read_text()
    assert "GNS3-SEAL-COMPLETE" in (store.path(job_id) / "console.log").read_text()

    (tmp / "vm-off").unlink()
    no_pw = store.create(pb.make_job(form(recipe="manual-linux", name="Self"), pb.load_recipes(), ["ubuntu-base.qcow2"], "admin"))
    thread = run_in_background(module.Build(store.path(no_pw)))
    assert wait_for(lambda: "shut the VM down from its console" in store.status(no_pw)["message"])
    (store.path(no_pw) / "finish").touch()
    assert wait_for(lambda: "needs the login password" in (store.path(no_pw) / "console.log").read_text())
    (tmp / "vm-off").touch()
    thread.join(30)
    assert store.status(no_pw)["state"] == "succeeded" and "not sealed by the builder" in (store.path(no_pw) / "console.log").read_text()


@pytest.fixture
def vnc_server():
    """A stand-in for the VM's VNC port: echoes what it receives, after sending a greeting."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()

    def echo(connection):
        with connection:
            connection.sendall(b"RFB 003.008\n")
            while data := connection.recv(4096):
                connection.sendall(data)

    def serve():   # every connection gets the greeting, so a relay that should have refused fails the test instead of hanging
        while True:
            try:
                connection, _ = listener.accept()
            except OSError:
                return
            threading.Thread(target=echo, args=(connection,), daemon=True).start()
    threading.Thread(target=serve, daemon=True).start()
    yield listener.getsockname()[1]
    listener.close()


def running_job(base, port):
    store = pb.JobStore(base)
    job_id = store.create(pb.make_job(form(recipe="manual-linux", name=f"Screen {port}"), pb.load_recipes(), ["ubuntu-base.qcow2"], "admin"))
    pb.write_json(store.path(job_id) / "status.json", {"state": "running", "message": "Configure", "percent": 40, "vnc_port": port})
    return job_id


def test_screen_is_relayed_only_to_an_admin_from_this_site(client, base, vnc_server):
    job_id = running_job(base, vnc_server)
    url = f"/admin/appliance-builder/{job_id}/screen"
    assert client.get(f"/admin/appliance-builder/{job_id}/log").json()["screen"] is True
    with client.websocket_connect(url, headers=ORIGIN, subprotocols=["binary"]) as ws:
        assert ws.receive_bytes() == b"RFB 003.008\n"
        ws.send_bytes(b"\x01\x02keys")
        assert ws.receive_bytes() == b"\x01\x02keys"
    for headers in ({"origin": "http://evil.example"}, {}):   # another site, or no Origin at all
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(url, headers=headers) as ws:
                ws.receive_bytes()
    other = TestClient(portal.app)
    other.post("/login", data={"username": "plain2", "password": "plain2-password-1"})
    with pytest.raises(WebSocketDisconnect):
        with other.websocket_connect(url, headers=ORIGIN) as ws:
            ws.receive_bytes()
    pb.write_json(pb.JobStore(base).path(job_id) / "status.json", {"state": "succeeded", "message": "done", "percent": 100, "vnc_port": vnc_server})
    with pytest.raises(WebSocketDisconnect):   # a finished build has no screen to relay
        with client.websocket_connect(url, headers=ORIGIN) as ws:
            ws.receive_bytes()


def test_pages_offer_isos_the_screen_and_an_optional_password_for_manual_builds(client, base):
    (base / "isos").mkdir()
    (base / "isos" / "ubuntu-desktop.iso").write_bytes(b"ISO")
    page = client.get("/admin/appliance-builder").text
    assert 'value="iso:ubuntu-desktop.iso"' in page and 'value="manual-windows"' in page
    response = client.post("/admin/appliance-builder", data=form(recipe="manual-linux", name="From ISO", baseline="iso:ubuntu-desktop.iso", login_password=""), follow_redirects=False)
    job_id = response.headers["location"].rsplit("/", 1)[1]
    assert "secret" not in [p.name for p in pb.JobStore(base).path(job_id).iterdir()]   # no password, nothing written
    page = client.get(f"/admin/appliance-builder/{job_id}").text
    assert "/static/novnc/core/rfb.js" in page and "Ctrl+Alt+Del" in page and "Seal and finish" in page
    assert "password" in client.post("/admin/appliance-builder", data=form(recipe="dvwa", name="Needs pw", login_password=""), follow_redirects=False).headers["location"]
    assert client.get("/static/novnc/core/rfb.js").status_code == 200
    client.post(f"/admin/appliance-builder/{job_id}/finish")
    assert not (pb.JobStore(base).path(job_id) / "finish").exists()   # only a running build can be finished
