#!/usr/bin/env python3
"""Appliance builder service. Runs as the GNS3 user (it needs /dev/kvm and the QEMU image folder).

Watches <builder dir>/jobs for folders that carry a `queued` marker and builds them one at a time:
clone the Ubuntu baseline -> boot it with QEMU/KVM -> run the recipe over SSH -> seal -> publish the qcow2
into GNS3's image folder and write the .gns3a next to the job. The portal shows console.log and status.json.
Standard library only.
"""
import hashlib
import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import portal_builder as pb  # noqa: E402

BASE = pb.builder_dir()
IMAGES = Path(os.getenv("GNS3_IMAGES_DIR", "").strip() or "/home/gns3/GNS3/images/QEMU")
SSH_WAIT, RECIPE_WAIT, SEAL_WAIT = 600, 3600, 300


class Cancelled(Exception):
    pass


class Build:
    def __init__(self, folder: Path):
        self.folder = folder
        self.job = json.loads((folder / "job.json").read_text())
        self.work = BASE / "work" / self.job["id"]
        self.log = (folder / "console.log").open("ab", buffering=0)
        self.qemu: subprocess.Popen | None = None

    def say(self, text: str) -> None:
        self.log.write((text.rstrip("\n") + "\n").encode())

    def status(self, state: str, message: str, percent: int) -> None:
        pb.write_json(self.folder / "status.json", {"state": state, "message": message, "percent": percent})
        self.say(f"== {message}")

    def check_cancel(self) -> None:
        if (self.folder / "cancel").exists():
            raise Cancelled()

    def run(self, *command: str, **kwargs) -> None:
        self.say("$ " + " ".join(shlex.quote(part) for part in command))
        done = subprocess.run(command, stdout=self.log, stderr=subprocess.STDOUT, **kwargs)
        if done.returncode:
            raise RuntimeError(f"{command[0]} failed (exit {done.returncode}); see the console above.")

    def ssh(self, port: int, key: Path, remote: str, stdin: Path | None = None, timeout: int = 60) -> int:
        command = ["ssh", "-i", str(key), "-p", str(port), "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                   "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", f"{self.job['login_user']}@127.0.0.1", remote]
        handle = stdin.open("rb") if stdin else subprocess.DEVNULL
        proc = subprocess.Popen(command, stdin=handle, stdout=self.log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + timeout
        try:
            while proc.poll() is None:
                self.check_cancel()
                if time.monotonic() > deadline:
                    proc.kill()
                    raise RuntimeError("A step took longer than its time limit.")
                time.sleep(1)
        finally:
            if stdin:
                handle.close()
        return proc.returncode

    def payload(self, recipe: dict) -> Path:
        """The script that runs as root in the VM: environment, packages, recipe, then the administrator's own script."""
        lines = ["#!/usr/bin/env bash", "set -euo pipefail", "export DEBIAN_FRONTEND=noninteractive", "touch /etc/gns3-lab-build"]
        lines += [f"export {name}={shlex.quote(value)}" for name, value in self.job["inputs"].items()]
        lines += ["apt-get update"]
        packages = sorted({*recipe["packages"], *self.job["packages"]})
        if packages:
            lines.append("apt-get install -y " + " ".join(shlex.quote(item) for item in packages))
        lines += ["echo '== Recipe'", recipe["script"], "echo '== Your script'", self.job["custom_script"], "echo '== Build steps finished'"]
        path = self.work / "payload.sh"
        path.write_text("\n".join(lines) + "\n")
        path.chmod(0o600)
        return path

    def boot(self, disk: Path, seed: Path, port: int) -> None:
        job = self.job
        command = ["qemu-system-x86_64", "-enable-kvm", "-cpu", "host", "-machine", "pc", "-m", str(job["ram"]), "-smp", str(job["cpus"]),
                   "-drive", f"file={disk},if=virtio,format=qcow2", "-drive", f"file={seed},media=cdrom,readonly=on",
                   "-netdev", f"user,id=n0,hostfwd=tcp:127.0.0.1:{port}-:22", "-device", "virtio-net-pci,netdev=n0",
                   "-display", "none", "-serial", f"file:{self.work / 'serial.log'}", "-monitor", "none"]
        self.say("$ " + " ".join(shlex.quote(part) for part in command))
        self.qemu = subprocess.Popen(command, stdout=self.log, stderr=subprocess.STDOUT)

    def wait_ssh(self, port: int, key: Path) -> None:
        deadline = time.monotonic() + SSH_WAIT
        while time.monotonic() < deadline:
            self.check_cancel()
            if self.qemu.poll() is not None:
                raise RuntimeError("The build VM stopped before SSH came up. Check that /dev/kvm is usable and the baseline boots.")
            try:
                if self.ssh(port, key, "true", timeout=15) == 0:
                    return
            except RuntimeError:
                pass
            time.sleep(5)
        raise RuntimeError("The build VM did not accept SSH in 10 minutes. Is cloud-init enabled in the baseline?")

    def finish_vm(self) -> None:
        deadline = time.monotonic() + SEAL_WAIT
        while self.qemu.poll() is None:
            self.check_cancel()
            if time.monotonic() > deadline:
                raise RuntimeError("The VM did not power off after sealing.")
            time.sleep(2)

    def stop_vm(self) -> None:
        if self.qemu and self.qemu.poll() is None:
            self.qemu.terminate()
            try:
                self.qemu.wait(20)
            except subprocess.TimeoutExpired:
                self.qemu.kill()

    def build(self) -> None:
        job, folder = self.job, self.folder
        recipe = pb.load_recipes().get(job["recipe"])
        if not recipe:
            raise RuntimeError("The recipe for this job no longer exists.")
        baseline = BASE / "baselines" / job["baseline"]
        target = IMAGES / job["image"]
        if target.exists():
            raise RuntimeError(f"{target} already exists. Build with another name or version; an existing disk is never replaced.")
        self.work.mkdir(parents=True, mode=0o700)
        disk, seed, key = self.work / "disk.qcow2", self.work / "seed.iso", self.work / "key"
        self.status("running", "Cloning the Ubuntu baseline", 5)
        self.run("qemu-img", "convert", "-O", "qcow2", str(baseline), str(disk))
        self.run("qemu-img", "check", str(disk))
        self.run("qemu-img", "resize", str(disk), f"{job['disk_gb']}G")   # also grows: the guest expands its filesystem on boot
        self.status("running", "Preparing temporary build access", 12)
        self.run("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "gns3-lab-builder", "-f", str(key))
        seed_dir = self.work / "seed"
        seed_dir.mkdir()
        user = job["login_user"]
        (seed_dir / "user-data").write_text("#cloud-config\npreserve_hostname: true\nusers:\n  - name: %s\n    lock_passwd: false\n    sudo: 'ALL=(ALL) NOPASSWD:ALL'\n    ssh_authorized_keys:\n      - %s\n" % (user, (self.work / "key.pub").read_text().strip()))
        (seed_dir / "meta-data").write_text(f"instance-id: build-{job['id']}\n")
        self.run("xorriso", "-as", "mkisofs", "-quiet", "-J", "-r", "-V", "cidata", "-o", str(seed), str(seed_dir))
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        self.status("running", "Starting the build VM", 18)
        self.boot(disk, seed, port)
        self.status("running", "Waiting for the VM to accept SSH", 25)
        self.wait_ssh(port, key)
        self.status("running", "Installing the application (this can take a while)", 35)
        if self.ssh(port, key, "sudo -n bash -s", stdin=self.payload(recipe), timeout=RECIPE_WAIT):
            raise RuntimeError("The install script failed. The output above shows the failing command.")
        self.status("running", "Sealing the image", 70)
        seal = self.work / "seal.sh"
        seal.write_text(f"export HOST_PREFIX={shlex.quote(recipe['host_prefix'])}\n" + (pb.ROOT / "deploy" / "builder" / "seal.sh").read_text())
        before = (folder / "console.log").stat().st_size
        self.ssh(port, key, "sudo -n bash -s", stdin=seal, timeout=SEAL_WAIT)   # the VM powers off, so SSH ends abruptly
        self.finish_vm()
        with (folder / "console.log").open("rb") as handle:
            handle.seek(before)
            if b"GNS3-SEAL-COMPLETE" not in handle.read():
                raise RuntimeError("Sealing did not complete; the disk was not published.")
        self.status("running", "Compressing and publishing the disk", 80)
        if shutil.disk_usage(IMAGES).free < disk.stat().st_size:
            raise RuntimeError(f"Not enough free space in {IMAGES}.")
        partial = IMAGES / (job["image"] + ".partial")
        self.run("qemu-img", "convert", "-c", "-O", "qcow2", str(disk), str(partial))
        self.run("qemu-img", "check", str(partial))
        digest = hashlib.md5()
        with partial.open("rb") as handle:
            for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
                digest.update(block)
        os.link(partial, target)   # refuses to replace an existing file
        partial.unlink()
        size = target.stat().st_size
        pb.write_json(folder / "result.gns3a", pb.descriptor(job, recipe, job["image"], digest.hexdigest(), size))
        self.status("succeeded", f"Built {job['image']} ({size // 1048576} MiB). Review and publish it to the appliance library.", 100)

    def execute(self) -> None:
        try:
            self.build()
        except Cancelled:
            self.status("cancelled", "Cancelled.", 0)
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            self.status("failed", str(exc)[:300], 0)
        finally:
            self.stop_vm()
            (IMAGES / (self.job["image"] + ".partial")).unlink(missing_ok=True)
            shutil.rmtree(self.work, ignore_errors=True)   # also removes the build key and the install payload
            self.log.close()
            (self.folder / "queued").unlink(missing_ok=True)


def recover() -> None:
    """A job left running by a crash or restart cannot be resumed: say so, and clear its work folder."""
    for folder in (BASE / "jobs").glob("*/"):
        try:
            state = json.loads((folder / "status.json").read_text()).get("state")
        except (OSError, ValueError):
            continue
        if state == "running":
            pb.write_json(folder / "status.json", {"state": "failed", "message": "The builder service stopped during this build. Start it again.", "percent": 0})
            (folder / "queued").unlink(missing_ok=True)
            shutil.rmtree(BASE / "work" / folder.name, ignore_errors=True)


def main() -> None:
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    for name in ("jobs", "work", "baselines"):
        (BASE / name).mkdir(parents=True, exist_ok=True)
    recover()
    print("builder service ready, watching", BASE / "jobs", flush=True)
    while True:
        waiting = sorted((p for p in (BASE / "jobs").glob("*/queued") if pb.JOB_ID.match(p.parent.name)), key=lambda p: p.stat().st_mtime)
        for marker in waiting[:1]:
            try:
                Build(marker.parent).execute()
            except (OSError, ValueError, KeyError) as exc:
                print("job", marker.parent.name, "could not start:", exc, flush=True)
                marker.unlink(missing_ok=True)
        if not waiting:
            time.sleep(3)


if __name__ == "__main__":
    main()
