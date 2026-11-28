#!/usr/bin/env python3
"""Appliance builder service. Runs as the GNS3 user (it needs /dev/kvm and the QEMU image folder).

Watches <builder dir>/jobs for folders that carry a `queued` marker and builds them one at a time:
check the host can hold it -> clone the Ubuntu baseline -> boot it with QEMU/KVM -> install, check and seal the
application over SSH (the recipe's install-app.sh, check-app.sh, seal-app.sh) -> seal Ubuntu -> publish the qcow2
into GNS3's image folder and write the .gns3a next to the job. The portal shows console.log and status.json.
Standard library only.
"""
import hashlib
import secrets
import json
import os
import shlex
from xml.sax.saxutils import escape
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import builder_sources  # noqa: E402
import portal_builder as pb  # noqa: E402

BASE = pb.builder_dir()
IMAGES = Path(os.getenv("GNS3_IMAGES_DIR", "").strip() or "/home/gns3/GNS3/images/QEMU")
# With libvirt the build VM appears in Cockpit's Virtual machines page with its graphical console (setup-builder.sh --cockpit).
LIBVIRT = os.getenv("PORTAL_BUILDER_LIBVIRT", "").strip().lower() in ("1", "true", "yes")
VIRSH = ["virsh", "-q", "-c", "qemu:///system"]
SSH_WAIT, RECIPE_WAIT, SEAL_WAIT = 600, 3600, 300


# Ubuntu starts apt-daily and unattended-upgrades right after boot, and they hold apt's locks. Pause their
# timers for this boot only (the image keeps them enabled), wait for a running one to finish, and let apt
# itself wait for its locks; seal.sh removes that apt setting again.
APT_QUIET = """systemctl stop apt-daily.timer apt-daily-upgrade.timer 2>/dev/null || true
printf 'DPkg::Lock::Timeout "600";\\n' > /etc/apt/apt.conf.d/90-gns3-build
for i in $(seq 1 360); do
  pgrep -x 'apt|apt-get|dpkg|unattended-upgr' >/dev/null || break
  [ "$i" = 1 ] && echo "Waiting for Ubuntu's automatic updates to finish..."
  sleep 5
done"""


# Printed in the console after the check, so the recipe's numbers can be tightened from a real run.
MEASURE = """echo "Disk used: $(df -h --output=used / | tail -1 | tr -d ' ')  Memory used: $(free -m | awk '/^Mem:/{print $3}') MiB of $(free -m | awk '/^Mem:/{print $2}') MiB" || true
echo "Listening TCP ports: $(ss -ltnH 2>/dev/null | awk '{print $4}' | sed 's/.*://' | sort -nu | tr '\\n' ' ')" || true
echo "Running services: $(systemctl list-units --type=service --state=running --no-legend --plain 2>/dev/null | awk '{print $1}' | tr '\\n' ' ')" || true
"""


class Cancelled(Exception):
    pass


def domain_xml(name: str, title: str, ram: int, cpus: int, disk: Path, port: int) -> str:
    """A transient libvirt VM equal to the plain QEMU one: same CPU, disk and user-mode network with SSH on 127.0.0.1:port,
    plus a VNC screen on 127.0.0.1 that Cockpit shows in the browser. It disappears when it powers off."""
    return f"""<domain type='kvm'>
  <name>{name}</name>
  <title>{escape(title)}</title>
  <description>GNS3 Lab Portal appliance build. Removed automatically when it powers off.</description>
  <memory unit='MiB'>{ram}</memory>
  <vcpu>{cpus}</vcpu>
  <os><type arch='x86_64' machine='pc'>hvm</type><boot dev='hd'/></os>
  <features><acpi/><apic/></features>
  <cpu mode='host-passthrough'/>
  <on_poweroff>destroy</on_poweroff>
  <on_reboot>restart</on_reboot>
  <devices>
    <disk type='file' device='disk'><driver name='qemu' type='qcow2'/><source file='{escape(str(disk))}'/><target dev='vda' bus='virtio'/></disk>
    <interface type='user'>
      <backend type='passt'/>
      <portForward proto='tcp' address='127.0.0.1'><range start='{port}' to='22'/></portForward>
      <model type='virtio'/>
    </interface>
    <graphics type='vnc' autoport='yes' listen='127.0.0.1'><listen type='address' address='127.0.0.1'/></graphics>
    <video><model type='virtio'/></video>
    <input type='tablet' bus='usb'/>
    <serial type='pty'/>
    <console type='pty'/>
  </devices>
</domain>
"""


def ssh_banner(port: int) -> bool:
    """True once sshd in the VM answers (its greeting starts with SSH-)."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as connection:
            connection.settimeout(5)
            return connection.recv(4).startswith(b"SSH-")
    except OSError:
        return False


def host_facts() -> tuple[int, int, set[str]]:
    """CPUs, available memory (MiB) and CPU flags of this server. The VM runs with -cpu host, so it gets the same flags."""
    meminfo = Path("/proc/meminfo").read_text()
    available = int(next(line.split()[1] for line in meminfo.splitlines() if line.startswith("MemAvailable:"))) // 1024
    flags = next((line.split(":", 1)[1].split() for line in Path("/proc/cpuinfo").read_text().splitlines() if line.startswith("flags")), [])
    return os.cpu_count() or 1, available, set(flags)


def preflight(job: dict, recipe: dict) -> None:
    """Refuse a build this server cannot hold, before any disk is copied or VM started."""
    if LIBVIRT and not shutil.which("virsh"):
        raise RuntimeError("PORTAL_BUILDER_LIBVIRT is on but libvirt is not installed. Run sudo bash scripts/setup-builder.sh --cockpit.")
    cpus, memory, flags = host_facts()
    if job["cpus"] > cpus:
        raise RuntimeError(f"The build VM needs {job['cpus']} CPUs; this server has {cpus}.")
    if job["ram"] > memory:
        raise RuntimeError(f"The build VM needs {job['ram']} MiB of memory; only {memory} MiB is free now. Stop some labs or lower the memory (minimum {recipe['min_ram']} MiB).")
    if missing := [flag for flag in recipe["cpu_flags"] if flag not in flags]:
        raise RuntimeError(f"{recipe['label']} needs CPU features this server lacks: {', '.join(missing)}.")
    need = recipe["min_free_gib"] * 1024**3
    (BASE / "work").mkdir(parents=True, exist_ok=True)
    for folder in {IMAGES, BASE / "work"}:
        free = shutil.disk_usage(folder).free
        if free < need:
            raise RuntimeError(f"{recipe['label']} needs {recipe['min_free_gib']} GiB free in {folder}; {free // 1024**3} GiB is free.")


class Build:
    def __init__(self, folder: Path):
        self.folder = folder
        self.job = json.loads((folder / "job.json").read_text())
        self.work = BASE / "work" / self.job["id"]
        self.log = (folder / "console.log").open("ab", buffering=0)
        secret = folder / "secret"
        try:
            self.password = secret.read_text().rstrip("\n")   # kept in memory only; never logged
        except OSError:
            self.password = ""
        secret.unlink(missing_ok=True)
        self.qemu: subprocess.Popen | None = None
        self.domain = ""   # set once a libvirt VM is created

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

    def stage(self, name: str, recipe: dict, steps: list[tuple[str, str]], before: list[str] | None = None) -> Path:
        """One root script for the VM. Each hook is written to a file and run on its own, so an exit in one cannot skip the next."""
        lines = ["#!/usr/bin/env bash", "set -euo pipefail", "export DEBIAN_FRONTEND=noninteractive", "touch /etc/gns3-lab-build",
                 "install -d -m 700 /root/.gns3-build", APT_QUIET]
        if self.job.get("license_accepted"):
            lines.append("export APPLIANCE_ACCEPT_LICENSE=1")
        lines += before or []
        for hook, text in steps:
            if not text.strip():
                continue
            end = "GNS3_HOOK_" + secrets.token_hex(8)   # a heredoc end marker no script can contain by accident
            lines += [f"echo '== {hook}'", f"cat > /root/.gns3-build/{hook} <<'{end}'", text.rstrip("\n"), end, f"bash /root/.gns3-build/{hook}"]
        path = self.work / name
        path.write_text("\n".join(lines) + "\n")
        path.chmod(0o600)
        return path

    def install_stage(self, recipe: dict) -> Path:
        packages = sorted({*recipe["packages"], *self.job["packages"]})
        apt = ["apt-get update"] + (["apt-get install -y " + " ".join(shlex.quote(item) for item in packages)] if packages else [])
        return self.stage("install.sh", recipe, [("install-app.sh", recipe["hooks"]["install-app.sh"]), ("your-script.sh", self.job["custom_script"])], apt)

    def seal_stage(self, recipe: dict) -> Path:
        stop = [f"systemctl stop {shlex.quote(service)}" for service in recipe["services"]]
        steps = [("check-app.sh", recipe["hooks"]["check-app.sh"]), ("stop-services.sh", "\n".join(stop)), ("seal-app.sh", recipe["hooks"]["seal-app.sh"]),
                 ("seal-ubuntu.sh", "rm -rf /root/.gns3-build\n" + f"export HOST_PREFIX={shlex.quote(recipe['host_prefix'])}\n" + (pb.ROOT / "deploy" / "builder" / "seal.sh").read_text())]
        return self.stage("seal.sh", recipe, steps)

    def boot(self, disk: Path, port: int) -> None:
        job = self.job
        if LIBVIRT:
            os.chmod(self.work, 0o711)   # libvirt's QEMU user must reach the disk; the key and scripts inside stay 0600
            xml = self.work / "domain.xml"
            name = f"gns3-build-{job['id'][:12]}"
            xml.write_text(domain_xml(name, f"GNS3 build: {job['name']} {job.get('version', '')}".strip(), job["ram"], job["cpus"], disk, port))
            self.run(*VIRSH, "create", str(xml))
            self.domain = name
            self.say(f"The VM is {self.domain} in Cockpit (Virtual machines): open it to see its screen.")
            return
        command = ["qemu-system-x86_64", "-enable-kvm", "-cpu", "host", "-machine", "pc", "-m", str(job["ram"]), "-smp", str(job["cpus"]),
                   "-drive", f"file={disk},if=virtio,format=qcow2",
                   "-netdev", f"user,id=n0,hostfwd=tcp:127.0.0.1:{port}-:22", "-device", "virtio-net-pci,netdev=n0",
                   "-display", "none", "-serial", f"file:{self.work / 'serial.log'}", "-monitor", "none"]
        self.say("$ " + " ".join(shlex.quote(part) for part in command))
        self.qemu = subprocess.Popen(command, stdout=self.log, stderr=subprocess.STDOUT)

    def wait_ssh(self, port: int, key: Path) -> None:
        deadline = time.monotonic() + SSH_WAIT
        while time.monotonic() < deadline:
            self.check_cancel()
            if not self.vm_running():
                raise RuntimeError("The build VM stopped before SSH came up. Check that /dev/kvm is usable and the baseline boots.")
            if ssh_banner(port):
                return
            time.sleep(5)
        raise RuntimeError("The build VM did not answer on SSH in 10 minutes. Does the baseline start OpenSSH at boot?")

    def bootstrap(self, port: int, key: Path) -> None:
        """Log in once with the baseline user's password, add the one-time key and a temporary sudo rule (seal removes both)."""
        user = self.job["login_user"]
        public = (self.work / "key.pub").read_text().strip()
        script = f"""set -e
home=$(getent passwd {user} | cut -d: -f6)
install -d -m 700 -o {user} -g "$(id -gn {user})" "$home/.ssh"
printf '%s\\n' {shlex.quote(public)} >> "$home/.ssh/authorized_keys"
chown {user}:"$(id -gn {user})" "$home/.ssh/authorized_keys"; chmod 600 "$home/.ssh/authorized_keys"
printf '%s ALL=(ALL) NOPASSWD:ALL\\n' {user} > /etc/sudoers.d/90-gns3-builder
chmod 440 /etc/sudoers.d/90-gns3-builder; visudo -cf /etc/sudoers.d/90-gns3-builder >/dev/null
echo "Build access added for {user}."
"""
        askpass = self.work / "askpass"
        askpass.write_text('#!/bin/sh\nprintf \'%s\\n\' "$GNS3_BUILD_PASSWORD"\n')
        askpass.chmod(0o700)
        # The password reaches ssh through askpass and sudo through stdin; it is in no file, argument or log.
        remote = f"IFS= read -r p; printf '%s\\n' \"$p\" | sudo -S -p '' bash -c {shlex.quote(script)}"
        command = ["ssh", "-p", str(port), "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", "-o", "PubkeyAuthentication=no",
                   "-o", "PreferredAuthentications=password,keyboard-interactive", "-o", "NumberOfPasswordPrompts=1", "-o", "ConnectTimeout=10",
                   f"{user}@127.0.0.1", remote]
        env = {**os.environ, "SSH_ASKPASS": str(askpass), "SSH_ASKPASS_REQUIRE": "force", "DISPLAY": os.environ.get("DISPLAY", ":0"), "GNS3_BUILD_PASSWORD": self.password}
        try:
            done = subprocess.run(command, input=(self.password + "\n").encode(), stdout=self.log, stderr=subprocess.STDOUT, env=env, timeout=120)
        except subprocess.TimeoutExpired:
            raise RuntimeError("Logging in to the build VM took longer than 2 minutes.") from None
        finally:
            askpass.unlink(missing_ok=True)
        if done.returncode != 0 or self.ssh(port, key, "sudo -n true", timeout=30) != 0:
            raise RuntimeError(f"Could not log in as {user} with the password given, or {user} cannot use sudo. Check the user name and password of the baseline.")

    def finish_vm(self) -> None:
        deadline = time.monotonic() + SEAL_WAIT
        while self.vm_running():
            self.check_cancel()
            if time.monotonic() > deadline:
                raise RuntimeError("The VM did not power off after sealing.")
            time.sleep(2)

    def vm_running(self) -> bool:
        if self.domain:   # a transient domain is gone once it powers off, so any error means not running
            done = subprocess.run([*VIRSH, "domstate", self.domain], capture_output=True, text=True)
            return done.returncode == 0 and done.stdout.strip() not in ("shut off", "crashed")
        return bool(self.qemu) and self.qemu.poll() is None

    def stop_vm(self) -> None:
        if self.domain:
            if self.vm_running():
                subprocess.run([*VIRSH, "destroy", self.domain], stdout=self.log, stderr=subprocess.STDOUT)
            return
        if self.qemu and self.qemu.poll() is None:
            self.qemu.terminate()
            try:
                self.qemu.wait(20)
            except subprocess.TimeoutExpired:
                self.qemu.kill()

    def build(self) -> None:
        job, folder = self.job, self.folder
        recipe = job["recipe_snapshot"]
        baseline = BASE / "baselines" / job["baseline"]
        target = IMAGES / job["image"]
        if target.exists():
            raise RuntimeError(f"{target} already exists. Build with another name or version; an existing disk is never replaced.")
        self.status("running", "Checking this server can hold the build", 2)
        preflight(job, recipe)
        self.work.mkdir(parents=True, mode=0o700)
        disk, key = self.work / "disk.qcow2", self.work / "key"
        if not self.password:
            raise RuntimeError("No password for the baseline user was given with this build.")
        self.status("running", "Cloning the Ubuntu baseline", 5)
        self.run("qemu-img", "convert", "-O", "qcow2", str(baseline), str(disk))
        self.run("qemu-img", "check", str(disk))
        current = json.loads(subprocess.run(["qemu-img", "info", "--output=json", str(disk)], capture_output=True, text=True, check=True).stdout)["virtual-size"]
        if job["disk_gb"] * 1024**3 > current:
            self.run("qemu-img", "resize", str(disk), f"{job['disk_gb']}G")   # the guest grows its filesystem on boot
        else:   # never shrink: that would cut off the baseline's filesystem
            self.say(f"Disk kept at {current // 1024**3} GiB: the baseline is already that size or larger, and a disk is never shrunk.")
        self.status("running", "Preparing temporary build access", 12)
        self.run("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "gns3-lab-builder", "-f", str(key))
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        self.status("running", "Starting the build VM", 18)
        self.boot(disk, port)
        self.status("running", "Waiting for the VM to accept SSH", 25)
        self.wait_ssh(port, key)
        self.status("running", "Logging in with the baseline user's password", 30)
        self.bootstrap(port, key)
        self.status("running", "Installing the application (this can take a while)", 35)
        if self.ssh(port, key, "sudo -n bash -s", stdin=self.install_stage(recipe), timeout=RECIPE_WAIT):
            raise RuntimeError("The install failed. The output above shows the failing command.")
        if recipe["hooks"]["check-app.sh"].strip():
            self.status("running", "Checking the application", 60)
            if self.ssh(port, key, "sudo -n bash -s", stdin=self.stage("check.sh", recipe, [("check-app.sh", recipe["hooks"]["check-app.sh"]), ("measure.sh", MEASURE)]), timeout=RECIPE_WAIT):
                raise RuntimeError("The application check failed, so nothing was published. The output above shows why.")
        self.status("running", "Sealing the image", 70)
        seal = self.seal_stage(recipe)
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
            if self.job.get("image"):
                (IMAGES / (self.job["image"] + ".partial")).unlink(missing_ok=True)
            shutil.rmtree(self.work, ignore_errors=True)   # also removes the build key and the install payload
            self.log.close()
            (self.folder / "queued").unlink(missing_ok=True)


class Analysis(Build):
    """Turn a pasted source into draft.json in the job folder; nothing is installed or published."""

    def build(self) -> None:
        self.status("running", "Looking at the source", 10)
        self.work.mkdir(parents=True, mode=0o700)
        recipes = pb.load_recipes(BASE / "recipes", include_hidden=True)
        builtin = {key for key, recipe in recipes.items() if recipe["source"] == "built-in"}
        try:
            result = builder_sources.draft(self.job["source"], self.work, self.say, set(recipes), builtin)
        except Exception as exc:   # network, HTTP and package errors all end here, with their message
            raise RuntimeError(f"Could not make a draft: {exc}") from exc
        for note in result["notes"]:
            self.say("note: " + note)
        pb.write_json(self.folder / "draft.json", result)
        self.status("succeeded", f"Draft ready: {result['settings']['label']}. Review it and save it as an application.", 100)


def recover() -> None:
    """A job left running by a crash or restart cannot be resumed: say so, and clear its work folder."""
    for folder in (BASE / "jobs").glob("*/"):
        (folder / "secret").unlink(missing_ok=True)
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
                kind = json.loads((marker.parent / "job.json").read_text()).get("kind")
                (Analysis if kind == "analyze" else Build)(marker.parent).execute()
            except (OSError, ValueError, KeyError) as exc:
                print("job", marker.parent.name, "could not start:", exc, flush=True)
                marker.unlink(missing_ok=True)
        if not waiting:
            time.sleep(3)


if __name__ == "__main__":
    main()
