"""Appliance builder: recipes, job folders and the .gns3a file.

Shared by the portal (which only writes job files and reads progress) and by the builder service
(scripts/appliance-builder.py, running as the GNS3 user, which owns /dev/kvm and does the VM work).
Standard library only, so the builder service needs no virtual environment.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
RECIPE_DIR = ROOT / "deploy" / "builder" / "recipes"
DEFAULT_BUILDER_DIR = "/var/lib/gns3-lab-portal-builder"

JOB_ID = re.compile(r"^[0-9a-f]{32}$")
TITLE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._+()-]{0,63}$")
PACKAGE = re.compile(r"^[a-z0-9][a-z0-9+.-]{0,63}$")
LOGIN = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
INPUT_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,40}$")
BASELINE_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.qcow2$")
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
MAX_SCRIPT, MAX_ICON, MAX_LOG_CHUNK = 64 * 1024, 1024 * 1024, 64 * 1024
FINISHED = frozenset({"succeeded", "failed", "cancelled"})


def builder_dir() -> Path:
    return Path(os.getenv("PORTAL_BUILDER_DIR", "").strip() or DEFAULT_BUILDER_DIR).expanduser()


def baselines(base: Path) -> list[str]:
    """Names of the Ubuntu baseline disks the administrator placed in <builder dir>/baselines."""
    folder = base / "baselines"
    try:
        return sorted(item.name for item in folder.iterdir() if BASELINE_FILE.match(item.name) and item.is_file())
    except OSError:
        return []


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "appliance"


def load_recipes(folder: Path = RECIPE_DIR) -> dict[str, dict[str, Any]]:
    """Every recipe is a folder: recipe.json, install.sh (optional) and icon.png (optional)."""
    recipes: dict[str, dict[str, Any]] = {}
    for item in sorted(folder.iterdir()) if folder.is_dir() else []:
        try:
            data = json.loads((item / "recipe.json").read_text())
        except (OSError, ValueError):
            continue
        script = item / "install.sh"
        icon = item / "icon.png"
        recipes[item.name] = {
            "id": item.name, "label": str(data.get("label") or item.name), "description": str(data.get("description") or ""),
            "packages": [str(p) for p in data.get("packages", []) if PACKAGE.match(str(p))],
            "inputs": [i for i in data.get("inputs", []) if isinstance(i, dict) and INPUT_NAME.match(str(i.get("name", "")))],
            "ram": int(data.get("ram", 2048)), "cpus": int(data.get("cpus", 2)), "disk_gb": int(data.get("disk_gb", 16)),
            "usage": str(data.get("usage") or ""), "host_prefix": slug(str(data.get("host_prefix") or item.name)),
            "script": script.read_text() if script.is_file() else "", "icon": icon if icon.is_file() else None,
        }
    return recipes


def _number(form: dict[str, str], key: str, label: str, default: int, low: int, high: int) -> int:
    raw = str(form.get(key) or "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        raise ValueError(f"{label} must be a whole number.") from None
    if not low <= value <= high:
        raise ValueError(f"{label} must be between {low} and {high}.")
    return value


def make_job(form: dict[str, str], recipes: dict[str, dict[str, Any]], available: list[str], created_by: str) -> dict[str, Any]:
    """Validate the request form into a job. Every value that reaches a root shell in the VM is checked here."""
    recipe = recipes.get(str(form.get("recipe", "")))
    if not recipe:
        raise ValueError("Choose an application.")
    name, version = str(form.get("name", "")).strip(), str(form.get("version", "")).strip() or "1"
    if not TITLE.match(name):
        raise ValueError("Give the appliance a name of up to 64 letters, digits, spaces or . _ + ( ) -.")
    if not TITLE.match(version):
        raise ValueError("The version may use up to 64 letters, digits, spaces or . _ + ( ) -.")
    if form.get("baseline") not in available:
        raise ValueError("Choose one of the baseline disks listed. Place the Ubuntu baseline in the baselines folder first.")
    login = str(form.get("login_user", "")).strip() or "labadmin"
    if not LOGIN.match(login):
        raise ValueError("The baseline's login user must be a normal Linux user name.")
    packages = [p for p in re.split(r"[\s,]+", str(form.get("packages", ""))) if p]
    if len(packages) > 50 or not all(PACKAGE.match(p) for p in packages):
        raise ValueError("Extra packages: up to 50 Ubuntu package names, separated by spaces or commas.")
    script = str(form.get("custom_script", "")).replace("\r\n", "\n")
    if len(script.encode()) > MAX_SCRIPT or "\x00" in script:
        raise ValueError("The custom script is limited to 64 KiB of text.")
    notes = str(form.get("notes", "")).strip()[:2000]
    inputs: dict[str, str] = {}
    for spec in recipe["inputs"]:
        value = str(form.get("input_" + spec["name"], "")).strip()
        if spec.get("kind") == "checkbox":
            value = "yes" if value else ""
        if not value and spec.get("required"):
            raise ValueError(f"{spec.get('label') or spec['name']} is required.")
        if "\n" in value or "\r" in value or "\x00" in value or len(value) > 2000:
            raise ValueError(f"{spec.get('label') or spec['name']} must be a single line of up to 2000 characters.")
        inputs[spec["name"]] = value
    return {
        "name": name, "version": version, "baseline": form["baseline"], "recipe": recipe["id"], "login_user": login,
        "packages": packages, "custom_script": script, "notes": notes, "inputs": inputs, "created_by": created_by,
        "cpus": _number(form, "cpus", "CPUs", recipe["cpus"], 1, 16),
        "ram": _number(form, "ram", "Memory (MiB)", recipe["ram"], 512, 65536),
        "disk_gb": _number(form, "disk_gb", "Disk size (GiB)", recipe["disk_gb"], 8, 512),
        "image": f"{slug(name)}-{slug(version)}.qcow2",
    }


def write_json(path: Path, data: Any) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, indent=2))
    os.replace(temporary, path)


class JobStore:
    """One folder per build under <builder dir>/jobs. The portal creates it; the builder service fills it."""

    def __init__(self, base: Path):
        self.root = base / "jobs"

    def path(self, job_id: str) -> Path:
        if not JOB_ID.match(job_id):
            raise KeyError(job_id)
        return self.root / job_id

    def create(self, job: dict[str, Any], icon: bytes | None = None) -> str:
        if icon is not None and (not icon.startswith(PNG_MAGIC) or len(icon) > MAX_ICON):
            raise ValueError("The icon must be a PNG image of 1 MiB or less.")
        job_id = uuid.uuid4().hex
        folder = self.path(job_id)
        folder.mkdir(parents=True, mode=0o2775)
        os.chmod(folder, 0o2775)   # the builder service (another user) writes here through the folder's ACL
        if icon is not None:
            (folder / "icon.png").write_bytes(icon)
        write_json(folder / "job.json", {**job, "id": job_id, "created": int(time.time())})
        (folder / "queued").touch()   # the builder service starts only jobs that carry this marker
        return job_id

    def get(self, job_id: str) -> dict[str, Any]:
        try:
            job = json.loads((self.path(job_id) / "job.json").read_text())
        except (OSError, ValueError):
            raise KeyError(job_id) from None
        return {**job, **self.status(job_id)}

    def status(self, job_id: str) -> dict[str, Any]:
        folder = self.path(job_id)
        try:
            data = json.loads((folder / "status.json").read_text())
            return {"state": str(data.get("state", "queued")), "message": str(data.get("message", "")), "percent": int(data.get("percent", 0))}
        except (OSError, ValueError):
            return {"state": "queued", "message": "Waiting for the builder service.", "percent": 0}

    def all(self, limit: int = 30) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        try:
            names = [item.name for item in self.root.iterdir() if JOB_ID.match(item.name)]
        except OSError:
            return []
        for name in names:
            try:
                found.append(self.get(name))
            except KeyError:
                continue
        return sorted(found, key=lambda item: item.get("created", 0), reverse=True)[:limit]

    def read_log(self, job_id: str, offset: int) -> tuple[str, int]:
        """Up to 64 KiB of console text from a byte offset, cut at a line end so a request never splits one."""
        try:
            with (self.path(job_id) / "console.log").open("rb") as handle:
                handle.seek(max(offset, 0))
                chunk = handle.read(MAX_LOG_CHUNK)
        except OSError:
            return "", max(offset, 0)
        if len(chunk) == MAX_LOG_CHUNK and b"\n" in chunk:
            chunk = chunk[: chunk.rindex(b"\n") + 1]
        return chunk.decode("utf-8", "replace"), max(offset, 0) + len(chunk)

    def cancel(self, job_id: str) -> None:
        (self.path(job_id) / "cancel").touch()


def descriptor(job: dict[str, Any], recipe: dict[str, Any], image: str, md5: str, size: int) -> dict[str, Any]:
    """The .gns3a appliance file for a finished build (registry version 6, QEMU)."""
    usage = "\n".join(part for part in (job.get("notes", ""), recipe.get("usage", ""), f"Built on {job['baseline']}. Sign in with the baseline's user ({job['login_user']}) and change its password.") if part)
    return {
        "appliance_id": str(uuid.uuid4()), "name": job["name"], "category": "guest", "description": recipe.get("description") or job["name"],
        "vendor_name": "Local lab", "product_name": job["name"], "registry_version": 6, "status": "experimental",
        "maintainer": "Local lab", "maintainer_email": "local-lab@example.invalid", "usage": usage[:2000],
        "linked_clone": True, "first_port_name": "Ethernet0", "port_name_format": "Ethernet{0}",
        "qemu": {"arch": "x86_64", "cpus": job["cpus"], "ram": job["ram"], "adapters": 1, "adapter_type": "virtio-net-pci",
                 "hda_disk_interface": "virtio", "console_type": "vnc", "boot_priority": "c", "kvm": "require",
                 "options": "-cpu host -machine pc -vga std", "on_close": "shutdown_signal"},
        "images": [{"filename": image, "version": job["version"], "md5sum": md5, "filesize": size}],
        "versions": [{"name": job["version"], "images": {"hda_disk_image": image}}],
    }
