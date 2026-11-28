"""Appliance builder: recipes, job folders and the .gns3a file.

Shared by the portal (which only writes job files and reads progress) and by the builder service
(scripts/appliance-builder.py, running as the GNS3 user, which owns /dev/kvm and does the VM work).
Standard library only, so the builder service needs no virtual environment.
"""
from __future__ import annotations

import io
import json
import math
import os
import posixpath
import re
import shutil
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
RECIPE_DIR = ROOT / "deploy" / "builder" / "recipes"
DEFAULT_BUILDER_DIR = "/var/lib/gns3-lab-portal-builder"

JOB_ID = re.compile(r"^[0-9a-f]{32}$")
TITLE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._+()-]{0,63}$")
PACKAGE = re.compile(r"^[a-z0-9][a-z0-9+.-]{0,63}$")
LOGIN = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
RECIPE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
CPU_FLAG = re.compile(r"^[a-z0-9_]{1,32}$")
SERVICE = re.compile(r"^[A-Za-z0-9@._-]{1,80}$")
HOOKS = ("install-app.sh", "check-app.sh", "seal-app.sh")
BASELINE_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.qcow2$")
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
MAX_SCRIPT, MAX_ICON, MAX_LOG_CHUNK = 64 * 1024, 1024 * 1024, 64 * 1024
MAX_RECIPE_ZIP, MAX_ZIP_MEMBERS, MAX_ZIP_UNPACKED = 2 * 1024 * 1024, 100, 4 * 1024 * 1024
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


def _whole(data: dict[str, Any], key: str, default: int, low: int, high: int) -> int:
    value = data.get(key, default)
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{key} must be a whole number from {low} to {high}.")
    return value


def recipe_settings(data: Any) -> dict[str, Any]:
    """Check a recipe.json, or a profile from ubuntu-appliance-generator, and return the recipe settings.

    Everything here ends up in the build form, the host checks or the appliance file, so all of it is validated.
    """
    if not isinstance(data, dict):
        raise ValueError("The recipe must be a JSON object.")
    if "display_name" in data:   # ubuntu-appliance-generator profile
        version = str(data.get("version") or "")
        data = {
            "label": data.get("display_name"), "description": data.get("description") or f"{data.get('display_name')} {version} on Ubuntu".strip(),
            "cpus": data.get("cpus", 2), "ram": data.get("ram", 2048), "disk_gb": data.get("disk_gib", 16),
            "min_cpus": data.get("min_cpus", 1), "min_ram": data.get("min_ram", 1024), "min_free_gib": math.ceil(data.get("min_free_gib", 4) or 0),
            "cpu_flags": data.get("cpu_flags", []), "requires_license": data.get("requires_license", False), "license_url": data.get("license_url") or data.get("vendor_url") or "",
            "services": data.get("services", []), "ports": data.get("ports", []), "usage": data.get("usage", ""), "host_prefix": data.get("id", ""),
        }
    label = str(data.get("label") or "").strip()
    if not label or len(label) > 80:
        raise ValueError("The recipe needs a label (display name) of up to 80 characters.")
    cpus, ram = _whole(data, "cpus", 2, 1, 16), _whole(data, "ram", 2048, 512, 65536)
    settings = {
        "label": label, "description": str(data.get("description") or "")[:300], "usage": str(data.get("usage") or "")[:2000],
        "cpus": cpus, "ram": ram, "disk_gb": _whole(data, "disk_gb", 16, 8, 512),
        "min_cpus": _whole(data, "min_cpus", 1, 1, cpus), "min_ram": _whole(data, "min_ram", 512, 512, ram),
        "min_free_gib": _whole(data, "min_free_gib", 4, 0, 1024),
        "requires_license": data.get("requires_license", False), "license_url": str(data.get("license_url") or ""),
        "custom": data.get("custom", False), "host_prefix": slug(str(data.get("host_prefix") or label))[:24],
    }
    for key in ("requires_license", "custom"):
        if not isinstance(settings[key], bool):
            raise ValueError(f"{key} must be true or false.")
    if settings["license_url"] and not settings["license_url"].startswith("https://"):
        raise ValueError("license_url must start with https://.")
    lists = {"packages": PACKAGE, "cpu_flags": CPU_FLAG, "services": SERVICE}
    for key, pattern in lists.items():
        value = data.get(key, [])
        if not isinstance(value, list) or len(value) > 50 or not all(isinstance(item, str) and pattern.match(item) for item in value):
            raise ValueError(f"{key} must be a list of plain names.")
        settings[key] = value
    ports = data.get("ports", [])
    if not isinstance(ports, list) or len(ports) > 20:
        raise ValueError("ports must be a list.")
    settings["ports"] = []
    for port in ports:
        if not isinstance(port, dict) or type(port.get("guest_port")) is not int or not 1 <= port["guest_port"] <= 65535:
            raise ValueError("Each port needs a guest_port from 1 to 65535.")
        settings["ports"].append({"name": slug(str(port.get("name") or "port"))[:24], "guest_port": port["guest_port"]})
    return settings


def _read_recipe(folder: Path, source: str) -> dict[str, Any]:
    settings = recipe_settings(json.loads((folder / "recipe.json").read_text()))
    hooks = {name: (folder / name).read_text() for name in HOOKS if (folder / name).is_file()}
    if not settings["custom"] and set(hooks) != set(HOOKS):
        raise ValueError("A recipe needs install-app.sh, check-app.sh and seal-app.sh.")
    icon = folder / "icon.png"
    return {**settings, "id": folder.name, "source": source, "hooks": hooks, "icon": icon if icon.is_file() else None}


def load_recipes(imported: Path | None = None) -> dict[str, dict[str, Any]]:
    """Built-in recipes from the repository, then the ones imported in the portal. A built-in id always wins."""
    recipes: dict[str, dict[str, Any]] = {}
    for root, source in ((RECIPE_DIR, "built-in"), (imported if imported is not None else builder_dir() / "recipes", "imported")):
        for folder in sorted(root.iterdir()) if root.is_dir() else []:
            if RECIPE_ID.match(folder.name) and folder.name not in recipes:
                try:
                    recipes[folder.name] = _read_recipe(folder, source)
                except (OSError, ValueError):
                    continue   # a broken recipe is left out; import refuses to create one
    return recipes


def import_recipe(raw: bytes, imported: Path, taken: set[str]) -> str:
    """Unpack an uploaded recipe zip into <builder dir>/recipes/<id>.

    The zip holds either recipe.json with install-app.sh, check-app.sh, seal-app.sh (and icon.png) beside it, or a
    profile from ubuntu-appliance-generator whose "hooks" and "icon" entries point at files inside the same zip.
    """
    if len(raw) > MAX_RECIPE_ZIP:
        raise ValueError("The recipe zip is larger than 2 MiB.")
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        raise ValueError("That is not a zip file.") from None
    with archive:
        members = {item.filename: item for item in archive.infolist() if not item.is_dir()}
        if len(members) > MAX_ZIP_MEMBERS or sum(item.file_size for item in members.values()) > MAX_ZIP_UNPACKED:
            raise ValueError("The recipe zip has too many or too large files.")
        candidates = [name for name in members if posixpath.basename(name) == "recipe.json"] or [name for name in members if name.endswith(".json")]
        if len(candidates) != 1:
            raise ValueError("The zip must contain exactly one recipe.json, or one generator profile (.json).")
        base = posixpath.dirname(candidates[0])

        def member(relative: str, limit: int) -> bytes:
            path = posixpath.normpath(posixpath.join(base, relative))
            if path.startswith(("../", "/")) or path == ".." or path not in members:
                raise ValueError(f"{relative} is not in the zip.")
            if members[path].file_size > limit:
                raise ValueError(f"{relative} is too large.")
            return archive.read(path)

        try:
            data = json.loads(member(posixpath.basename(candidates[0]), 256 * 1024))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("The recipe file is not valid JSON.") from None
        settings = recipe_settings(data)
        recipe_id = str(data.get("id") or slug(settings["label"]))
        if not RECIPE_ID.match(recipe_id):
            raise ValueError("The recipe id must be lowercase letters, digits and hyphens.")
        if recipe_id in taken:
            raise ValueError(f"A recipe called {recipe_id} already exists. Change the id in the file, or delete the imported one first.")
        named = data.get("hooks") if isinstance(data.get("hooks"), dict) else {name: name for name in HOOKS}
        hooks: dict[str, str] = {}
        for name in HOOKS:
            if not isinstance(named.get(name), str):
                raise ValueError(f"The recipe needs {name}. An application without per-machine identity can use a seal-app.sh that only echoes.")
            try:
                hooks[name] = member(named[name], MAX_SCRIPT).decode("utf-8")
            except UnicodeDecodeError:
                raise ValueError(f"{name} is not UTF-8 text.") from None
        icon_name = data.get("icon") if isinstance(data.get("icon"), str) else ("icon.png" if posixpath.join(base, "icon.png").lstrip("/") in members else "")
        icon = member(icon_name, MAX_ICON) if icon_name else b""
        if icon and not icon.startswith(PNG_MAGIC):
            icon = b""   # an SVG or other format is skipped; GNS3 shows its default icon
    settings.pop("custom")
    staging = imported / f".{recipe_id}.{uuid.uuid4().hex}"
    staging.mkdir(parents=True)
    try:
        write_json(staging / "recipe.json", settings)
        for name, text in hooks.items():
            (staging / name).write_text(text)
        if icon:
            (staging / "icon.png").write_bytes(icon)
        os.rename(staging, imported / recipe_id)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return recipe_id


def delete_recipe(imported: Path, recipe_id: str) -> None:
    if not RECIPE_ID.match(recipe_id) or not (imported / recipe_id).is_dir():
        raise KeyError(recipe_id)
    shutil.rmtree(imported / recipe_id)


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
    scripts = {}
    for key, label in (("custom_script", "Your setup script"), ("check_script", "The check script"), ("seal_script", "The clean-up script")):
        scripts[key] = str(form.get(key, "")).replace("\r\n", "\n")
        if len(scripts[key].encode()) > MAX_SCRIPT or "\x00" in scripts[key]:
            raise ValueError(f"{label} is limited to 64 KiB of text.")
    if recipe["custom"] and not scripts["seal_script"].strip():
        raise ValueError("A custom appliance needs a clean-up script that removes per-machine identity before sealing. Write true if the software keeps none.")
    if recipe["requires_license"] and not form.get("accept_license"):
        raise ValueError(f"Accept the {recipe['label']} licence to build it.")
    hooks = dict(recipe["hooks"])
    if recipe["custom"]:
        hooks = {"install-app.sh": "", "check-app.sh": scripts["check_script"], "seal-app.sh": scripts["seal_script"]}
    snapshot = {key: value for key, value in recipe.items() if key not in ("icon", "hooks")}
    return {
        "name": name, "version": version, "baseline": form["baseline"], "recipe": recipe["id"], "login_user": login,
        "packages": packages, "custom_script": scripts["custom_script"], "notes": str(form.get("notes", "")).strip()[:2000], "created_by": created_by,
        "license_accepted": bool(recipe["requires_license"]),
        "cpus": _number(form, "cpus", "CPUs", recipe["cpus"], recipe["min_cpus"], 16),
        "ram": _number(form, "ram", "Memory (MiB)", recipe["ram"], recipe["min_ram"], 65536),
        "disk_gb": _number(form, "disk_gb", "Disk size (GiB)", recipe["disk_gb"], 8, 512),
        "image": f"{slug(name)}-{slug(version)}.qcow2",
        "recipe_snapshot": {**snapshot, "hooks": hooks},   # exactly what runs, even if the recipe changes before the build starts
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
