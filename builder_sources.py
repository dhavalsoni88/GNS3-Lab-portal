"""Turn a pasted source into a draft application for the appliance builder.

Accepted sources:
- a .deb download link over https (or the wget/curl command that fetches it),
- a GitHub repository link (it is installed from a .deb release, docker compose, a Dockerfile or install.sh),
- Ubuntu package names.

The builder service runs this (it has the disk space and the network); the result is a draft that an
administrator reviews before saving. Requirements are estimated from what the source declares. What a
package cannot say (vendor minimums, CPU features, per-clone clean-up) is left for the review.
Standard library only.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import re
import shlex
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

import portal_builder as pb

URL = re.compile(r"https?://[^\s'\"<>]+")
GITHUB = re.compile(r"^https://github\.com/([A-Za-z0-9][A-Za-z0-9-]{0,38})/([A-Za-z0-9._-]{1,100}?)(?:\.git)?(?:/(?:tree|blob|releases)(?:/.*)?)?/?$")
SERVICE_FILE = re.compile(r"\./(?:usr/)?lib/systemd/system/([A-Za-z0-9._-]+\.service)$")
COMPOSE_PORT = re.compile(r"""^\s*-\s*["']?(?:[\d.]+:)?(\d{1,5})(?:-\d+)?:\d{1,5}(?:/tcp)?["']?\s*$""", re.M)
EXPOSE = re.compile(r"^\s*EXPOSE\s+(.+)$", re.M | re.I)
OPEN_LICENCES = {"mit", "apache-2.0", "gpl-2.0", "gpl-3.0", "agpl-3.0", "lgpl-2.1", "lgpl-3.0", "bsd-2-clause", "bsd-3-clause",
                 "mpl-2.0", "isc", "unlicense", "epl-2.0", "cc0-1.0", "zlib", "bsl-1.0", "artistic-2.0"}
COMPOSE_FILES = ("compose.yaml", "compose.yml", "docker-compose.yml", "docker-compose.yaml")
INSTALL_FILES = ("install.sh", "setup.sh")
MAX_DOWNLOAD = 16 * 1024**3
GIB = 1024**3
SEAL_REVIEW = """# Review before saving: remove anything that must differ between clones (generated IDs, keys, accounts,
# licences, data). Nothing was detected automatically, so this does nothing yet.
true
"""


def classify(text: str) -> tuple[str, str]:
    """('deb', url) | ('github', 'owner/repo') | ('apt', 'name name'). Raises ValueError with what to paste instead."""
    text = text.strip()
    urls = URL.findall(text)
    if urls:
        url = urls[0].rstrip(").,;")
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "https":
            raise ValueError("Use an https:// link.")
        host = (parts.hostname or "").lower()
        if host in ("github.com", "www.github.com"):
            match = GITHUB.match(url.replace("://www.", "://"))
            if not match:
                raise ValueError("Use the repository's own link, like https://github.com/owner/repo.")
            return "github", f"{match[1]}/{match[2]}"
        try:
            private = ipaddress.ip_address(host).is_private or ipaddress.ip_address(host).is_loopback or ipaddress.ip_address(host).is_link_local
        except ValueError:
            private = host == "localhost" or host.endswith(".local") or "." not in host
        if private:
            raise ValueError("The link must point to a public download site.")
        if not parts.path.endswith(".deb"):
            raise ValueError("Links must be a GitHub repository or a .deb package download.")
        return "deb", url
    names = text.replace(",", " ").split()
    if names and len(names) <= 20 and all(pb.PACKAGE.match(name) for name in names):
        return "apt", " ".join(names)
    raise ValueError("Paste a GitHub repository link, a .deb download link (or the wget command that fetches it), or Ubuntu package names.")


# --- network: the only part that talks to the internet; tests replace fetch_json and download ---------------

def _open(url: str, timeout: int = 60):
    request = urllib.request.Request(url, headers={"User-Agent": "gns3-lab-portal-builder", "Accept": "application/vnd.github+json"})
    response = urllib.request.urlopen(request, timeout=timeout)   # noqa: S310 - https only, checked below and in classify
    if not response.geturl().startswith("https://"):
        response.close()
        raise ValueError(f"{url} redirected to a non-https address.")
    return response


def fetch_json(url: str) -> Any:
    with _open(url) as response:
        return json.loads(response.read(2 * 1024 * 1024))


def fetch_text(url: str) -> str:
    with _open(url) as response:
        return response.read(512 * 1024).decode("utf-8", "replace")


def download(url: str, target: Path, say: Callable[[str], None]) -> tuple[str, int]:
    """Stream to target; return (sha256, size). Progress goes to the console every 100 MiB."""
    digest, size, shown = hashlib.sha256(), 0, 0
    with _open(url, timeout=120) as response, target.open("xb") as output:
        while block := response.read(1024 * 1024):
            size += len(block)
            if size > MAX_DOWNLOAD:
                raise ValueError("The download is larger than 16 GiB.")
            digest.update(block)
            output.write(block)
            if size - shown >= 100 * 1024**2:
                shown = size
                say(f"downloaded {size // 1024**2} MiB")
    say(f"downloaded {size // 1024**2} MiB, sha256 {digest.hexdigest()}")
    return digest.hexdigest(), size


# --- facts from a downloaded .deb and from apt ------------------------------------------------------------

def deb_facts(path: Path) -> dict[str, Any]:
    fields = ("Package", "Version", "Architecture", "Depends", "Installed-Size", "Description", "Homepage")
    info = subprocess.run(["dpkg-deb", "-f", str(path), *fields], capture_output=True, text=True, check=True).stdout
    facts: dict[str, Any] = {}
    for line in info.splitlines():
        if ":" in line and not line.startswith(" "):
            key, value = line.split(":", 1)
            facts[key.strip()] = value.strip()
    listing = subprocess.run(["dpkg-deb", "-c", str(path)], capture_output=True, text=True, check=True).stdout
    facts["services"] = sorted({m[1] for line in listing.splitlines() if (m := SERVICE_FILE.search(line.split()[-1] if line.split() else ""))})
    return facts


def apt_facts(names: list[str]) -> dict[str, dict[str, str]]:
    """What this server's package lists say about each name (empty for names they do not know)."""
    found: dict[str, dict[str, str]] = {}
    for name in names:
        result = subprocess.run(["apt-cache", "show", "--no-all-versions", name], capture_output=True, text=True)
        fields: dict[str, str] = {}
        for line in result.stdout.splitlines():
            if not line.strip():
                break
            if ":" in line and not line.startswith(" "):
                key, value = line.split(":", 1)
                fields[key.strip()] = value.strip()
        found[name] = fields
    return found


# --- requirements ---------------------------------------------------------------------------------------

def sizing(installed_bytes: int, download_bytes: int = 0, ram: int = 2048) -> dict[str, int]:
    """Disk, free space and memory from what the package says it needs, with room for the OS and logs."""
    installed = installed_bytes / GIB
    if installed > 1:
        ram = max(ram, 4096)
    return {
        "cpus": 2, "min_cpus": 1, "ram": ram, "min_ram": min(ram, 1024 if ram <= 2048 else ram // 2),
        "disk_gb": max(16, 8 * math.ceil((installed * 2 + 8) / 8)),
        "min_free_gib": max(4, math.ceil(download_bytes / GIB + installed * 2 + 4)),
    }


def _unique_id(name: str, taken: set[str]) -> str:
    base = pb.slug(name)[:36] or "app"
    candidate, n = base, 2
    while candidate in taken:
        candidate, n = f"{base}-{n}", n + 1
    return candidate


def _wait_for_services(services: list[str]) -> str:
    if not services:
        return ""
    units = " ".join(shlex.quote(s) for s in services)
    return f"""for unit in {units}; do
  for _ in $(seq 1 60); do systemctl is-active --quiet "$unit" && break; sleep 2; done
  systemctl is-active --quiet "$unit" || {{ echo "$unit is not running: journalctl -u $unit" >&2; exit 1; }}
done
"""


# --- drafts per source ----------------------------------------------------------------------------------

def deb_draft(url: str, work: Path, say: Callable[[str], None], taken: set[str], builtin: set[str]) -> dict[str, Any]:
    path = work / "package.deb"
    say(f"Downloading {url}")
    sha256, size = download(url, path, say)
    facts = deb_facts(path)
    say("Package facts: " + json.dumps({k: v for k, v in facts.items() if k != "Description"}))
    if facts.get("Architecture") not in ("amd64", "all"):
        raise ValueError(f"The package is for {facts.get('Architecture') or 'an unknown architecture'}; the baseline is amd64.")
    package, version = facts.get("Package", "app"), facts.get("Version", "")
    if not pb.PACKAGE.match(package):
        raise ValueError("The package name is not a plain Debian package name.")
    services = [s for s in facts["services"] if pb.SERVICE.match(s)]
    installed = int(facts.get("Installed-Size") or 0) * 1024
    q = shlex.quote
    install = f"""# Generated from {url}
set -euo pipefail
apt-get install -y ca-certificates curl
download=$(mktemp -d); chmod 755 "$download"; trap 'rm -rf -- "$download"' EXIT
curl --fail --location --retry 3 --connect-timeout 30 --proto '=https' --tlsv1.2 -o "$download/app.deb" {q(url)}
echo {q(sha256 + '  ')}"$download/app.deb" | sha256sum -c -
apt-get install -y "$download/app.deb"   # also installs its dependencies: {facts.get('Depends', 'none')}
""" + "".join(f"systemctl enable --now {q(s)}\n" for s in services)
    check = f"""set -euo pipefail
dpkg-query -W -f='${{Status}} ${{Version}}\\n' {q(package)} | grep -q '^install ok installed'
{_wait_for_services(services)}echo "PASS: {package} is installed{' and its services run' if services else ''}."
"""
    notes = [f"Package {package} {version}, {size // 1024**2} MiB download, about {installed // 1024**2} MiB installed, pinned by SHA-256.",
             f"Dependencies (installed by apt): {facts.get('Depends') or 'none declared'}."]
    if services:
        notes.append("Services in the package, started and checked: " + ", ".join(services) + ".")
    else:
        notes.append("The package ships no systemd service. If the application needs a first start or a setup command, add it to install-app.sh or document it in Usage.")
    if package in builtin:
        notes.append(f"A built-in recipe for {package} exists and already knows its vendor requirements and per-clone setup. Prefer it.")
    notes.append("Vendor requirements (minimum memory, CPU features) are not in a .deb: check the vendor's system requirements and set them here.")
    return {
        "id": _unique_id(package, taken),
        "settings": {"label": f"{package} {version}".strip(), "description": (facts.get("Description") or package)[:300],
                     **sizing(installed, size), "cpu_flags": [], "requires_license": True, "license_url": facts.get("Homepage", "") if facts.get("Homepage", "").startswith("https://") else "",
                     "services": services, "ports": [], "packages": [], "host_prefix": package,
                     "usage": f"{package} {version}, installed from the vendor package {url.rsplit('/', 1)[-1]}."},
        "hooks": {"install-app.sh": install, "check-app.sh": check, "seal-app.sh": SEAL_REVIEW},
        "notes": notes,
    }


def apt_draft(names: list[str], say: Callable[[str], None], taken: set[str]) -> dict[str, Any]:
    facts = apt_facts(names)
    unknown = [name for name, fields in facts.items() if not fields]
    installed = sum(int(fields.get("Installed-Size") or 0) * 1024 for fields in facts.values())
    say("apt facts: " + json.dumps({name: {k: fields.get(k) for k in ("Version", "Installed-Size", "Depends")} for name, fields in facts.items()}))
    packages = " ".join(shlex.quote(n) for n in names)
    owned_units = f"dpkg -L {packages} 2>/dev/null | grep -E '/systemd/system/[^/@]+\\.service$' | xargs -r -n1 basename | sort -u"
    first = facts[names[0]]
    notes = [f"Installed with apt: {' '.join(names)}. About {installed // 1024**2} MiB according to this server's package lists."]
    if unknown:
        notes.append("Not in this server's package lists: " + ", ".join(unknown) + ". The baseline's Ubuntu release may still have them; the build will tell.")
    notes.append("Services the packages install are checked after the install and stopped before sealing.")
    return {
        "id": _unique_id(names[0], taken),
        "settings": {"label": " + ".join(names)[:80], "description": (first.get("Description") or first.get("Description-en") or "Ubuntu packages: " + ", ".join(names))[:300],
                     **sizing(installed), "cpu_flags": [], "requires_license": False, "license_url": "", "services": [], "ports": [],
                     "packages": names, "host_prefix": names[0], "usage": "Ubuntu packages: " + ", ".join(names) + "."},
        "hooks": {
            "install-app.sh": "set -euo pipefail\n# The packages are installed before this script (recipe packages). Add configuration here.\necho \"Installed: $(dpkg-query -W -f='${Package} ${Version}  ' " + packages + ")\"\n",
            "check-app.sh": f"set -euo pipefail\nfor package in {packages}; do dpkg-query -W -f='${{Status}}\\n' \"$package\" | grep -q '^install ok installed'; done\n"
                            f"for unit in $({owned_units}); do\n  systemctl is-enabled --quiet \"$unit\" || continue\n"
                            "  for _ in $(seq 1 30); do systemctl is-active --quiet \"$unit\" && break; sleep 2; done\n"
                            "  systemctl is-active --quiet \"$unit\" || { echo \"$unit is not running\" >&2; exit 1; }\ndone\necho \"PASS: packages installed, their services run.\"\n",
            "seal-app.sh": f"set -euo pipefail\nfor unit in $({owned_units}); do systemctl stop \"$unit\" || true; done\n" + SEAL_REVIEW,
        },
        "notes": notes,
    }


def github_draft(repo: str, work: Path, say: Callable[[str], None], taken: set[str], builtin: set[str]) -> dict[str, Any]:
    api = f"https://api.github.com/repos/{repo}"
    info = fetch_json(api)
    repo = info.get("full_name") or repo
    name = repo.split("/", 1)[1]
    branch = info.get("default_branch") or "main"
    commit = fetch_json(f"{api}/commits/{urllib.parse.quote(branch)}")["sha"]
    files = {item["name"] for item in fetch_json(f"{api}/contents?ref={commit}") if isinstance(item, dict) and item.get("type") == "file"}
    say(f"Repository {repo}, branch {branch} at {commit[:12]}, files: {', '.join(sorted(files)[:40])}")
    licence = ((info.get("license") or {}).get("spdx_id") or "").lower()
    licence_fields = {"requires_license": licence not in OPEN_LICENCES, "license_url": info.get("html_url", "")}
    licence_note = f"Licence: {licence.upper() or 'none declared'}." + ("" if licence in OPEN_LICENCES else " Not a common open-source licence, so the form asks for acceptance.")
    base = {"description": (info.get("description") or repo)[:300], "host_prefix": name, "cpu_flags": [], "packages": []}

    try:
        release = fetch_json(f"{api}/releases/latest")
    except Exception:   # no releases: GitHub answers 404
        release = {}
    debs = [a for a in release.get("assets", []) if isinstance(a, dict) and str(a.get("name", "")).endswith(".deb")
            and ("amd64" in a["name"] or "_all" in a["name"]) and str(a.get("browser_download_url", "")).startswith("https://")]
    if debs:
        say(f"Using the .deb from release {release.get('tag_name')}: {debs[0]['name']}")
        draft = deb_draft(debs[0]["browser_download_url"], work, say, taken, builtin)
        draft["settings"].update(licence_fields)
        draft["notes"].insert(0, f"From {repo} release {release.get('tag_name')}. {licence_note}")
        return draft

    q = shlex.quote
    folder = f"/opt/{pb.slug(name)}"
    unit = f"{pb.slug(name)}-app.service"
    clone = f"""set -euo pipefail
[[ ! -e {folder} ]] || {{ echo "{folder} already exists; use a clean baseline." >&2; exit 1; }}
git clone --quiet https://github.com/{repo}.git {folder}
git -C {folder} checkout --quiet --detach {commit}
"""
    compose = next((f for f in COMPOSE_FILES if f in files), "")
    if compose or "Dockerfile" in files:
        text = fetch_text(f"https://raw.githubusercontent.com/{repo}/{commit}/{compose or 'Dockerfile'}")
        if compose:
            ports = sorted({int(p) for p in COMPOSE_PORT.findall(text) if 0 < int(p) < 65536})
            start, stop, kind = f"/usr/bin/docker compose -f {compose} up -d --build", f"/usr/bin/docker compose -f {compose} down", "oneshot"
            running = f"docker compose -f {q(compose)} ps --status running -q | grep -q ."
        else:
            ports = sorted({int(p.split("/")[0]) for line in EXPOSE.findall(text) for p in line.split() if p.split("/")[0].isdigit()})
            image = f"gns3-lab/{pb.slug(name)}:{commit[:12]}"
            publish = " ".join(f"-p {p}:{p}" for p in ports)
            start, stop, kind = f"/usr/bin/docker run --rm --name {pb.slug(name)} {publish} {image}".replace("  ", " "), f"/usr/bin/docker stop {pb.slug(name)}", "simple"
            running = f"docker ps -q --filter name=^{pb.slug(name)}$ | grep -q ."
        build_image = "" if compose else f"docker build -t {q(image)} {folder}\n"
        install = clone + build_image + f"""cat > /etc/systemd/system/{unit} <<'UNIT'
[Unit]
Description={repo} (docker)
Requires=docker.service
After=docker.service network-online.target
[Service]
Type={kind}
{"RemainAfterExit=yes" if kind == "oneshot" else "Restart=on-failure"}
WorkingDirectory={folder}
ExecStart={start}
ExecStop={stop}
TimeoutStartSec=1800
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now {unit}
"""
        check = f"""set -euo pipefail
cd {folder}
for _ in $(seq 1 90); do {running} && break; sleep 2; done
{running} || {{ echo "The containers are not running: journalctl -u {unit}" >&2; exit 1; }}
""" + "".join(f"for _ in $(seq 1 60); do curl -fs -o /dev/null http://127.0.0.1:{p}/ && break; ss -ltn | grep -q ':{p} ' && break; sleep 2; done\n" for p in ports) + f'echo "PASS: {repo} runs in docker."\n'
        notes = [f"From {repo} at commit {commit[:12]}, run with docker {'compose (' + compose + ')' if compose else '(Dockerfile)'} as {unit}. {licence_note}",
                 "Container images are pulled or built during the build and kept in the appliance, so clones start without internet.",
                 ("Ports found: " + ", ".join(map(str, ports)) + ".") if ports else "No published ports found; check the application's documentation."]
        return {"id": _unique_id(name, taken),
                "settings": {**base, "label": name[:80], **sizing(info.get("size", 0) * 1024 * 4, ram=4096), "min_free_gib": 10, "disk_gb": 32, **licence_fields,
                             "services": [unit], "ports": [{"name": f"port-{p}", "guest_port": p} for p in ports],
                             "packages": ["docker.io", "docker-compose-v2", "git", "curl"],
                             "usage": f"{repo} at commit {commit[:12]}, started by {unit} at boot." + (f" Ports: {', '.join(map(str, ports))}." if ports else "")},
                "hooks": {"install-app.sh": install, "check-app.sh": check,
                          "seal-app.sh": "# docker compose down / docker stop ran before this (the service is stopped). Named volumes keep their data:\n# remove them here if they hold per-instance state.\n" + SEAL_REVIEW},
                "notes": notes}

    script = next((f for f in INSTALL_FILES if f in files), "")
    if script:
        return {"id": _unique_id(name, taken),
                "settings": {**base, "label": name[:80], **sizing(info.get("size", 0) * 1024 * 4), **licence_fields, "services": [], "ports": [],
                             "packages": ["git"], "usage": f"{repo} at commit {commit[:12]}, installed with its {script}."},
                "hooks": {"install-app.sh": clone + f"cd {folder}\nbash ./{script}\n",
                          "check-app.sh": "# Nothing could be inferred to check. Replace this with a real test, for example curl -fs http://127.0.0.1:PORT/\ntrue\n",
                          "seal-app.sh": SEAL_REVIEW},
                "notes": [f"From {repo} at commit {commit[:12]}, installed by running its {script} as root. {licence_note}",
                          "Read that script in the repository: it runs as root in the build VM. Write a real check-app.sh."]}
    raise ValueError(f"{repo} has no .deb release, compose file, Dockerfile or install.sh at its top level, so there is no safe way to guess how to install it. Use the Custom application instead.")


def draft(source: str, work: Path, say: Callable[[str], None], taken: set[str], builtin: set[str]) -> dict[str, Any]:
    kind, value = classify(source)
    if kind == "deb":
        result = deb_draft(value, work, say, taken, builtin)
    elif kind == "github":
        result = github_draft(value, work, say, taken, builtin)
    else:
        result = apt_draft(value.split(), say, taken)
    pb.recipe_settings(result["settings"])   # the draft must be a valid recipe before anyone sees it
    result["source"] = source.strip()[:500]
    return result
