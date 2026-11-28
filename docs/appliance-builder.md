# Appliance builder

**Administration › Appliance builder** (Platform Admin, permission `appliances.publish`) makes a GNS3
appliance from your Ubuntu baseline: you choose what goes on it, the portal builds the disk (qcow2) and
the `.gns3a` file, shows the build console live, and hands the result to the normal
[appliance publishing](admin-guide.md) page, which registers it for every lab.

## How it works

1. The builder service checks that this server can hold the build: enough CPUs, free memory and free disk,
   and the CPU features the application needs. If not, it stops before copying anything.
2. It clones your baseline disk (the baseline is never changed).
3. It boots the clone with QEMU/KVM, using a temporary SSH key delivered by cloud-init.
4. It installs the packages, then the application's `install-app.sh`, then your own script, as root inside
   the VM.
5. It runs the application's `check-app.sh`. If the check fails, nothing is published.
6. It runs the check again, stops the application's services, runs `seal-app.sh` (removes anything that
   must differ per clone), then seals Ubuntu: new machine ID, SSH host keys and host name for every clone,
   and the temporary key and passwordless sudo are removed. The VM powers off.
5. It compresses the disk into GNS3's QEMU image folder (`GNS3_IMAGES_DIR`), writes the `.gns3a` with
   its checksum and size, and the job turns *succeeded*. **Review and publish** opens the usual review page
   (template name, notes, QEMU binary) and then **Publish to all labs**. The icon goes in with it.

The portal never runs the VM. A separate service, `gns3-lab-portal-builder`, runs as the GNS3 user (it needs
`/dev/kvm` and the image folder). Portal and service exchange job files in
`/var/lib/gns3-lab-portal-builder` (`PORTAL_BUILDER_DIR`).

## One-time setup

```bash
sudo bash scripts/setup-builder.sh        # --build-user gns3 --service-user www-data are the defaults
```

It installs QEMU, xorriso and ssh, creates the folders, and starts the service. Then put your
**baseline** in `/var/lib/gns3-lab-portal-builder/baselines/`: a stopped, application-free Ubuntu server
disk (`.qcow2`, standalone, cloud-init enabled, with a normal user such as `labadmin`). Name the user on
the build form if it is not `labadmin`.

## Applications (recipes)

Each application is a *recipe*: a folder with

| File | What it does |
|---|---|
| `recipe.json` | Name, description, default CPU/memory/disk, and what the application needs (below) |
| `install-app.sh` | Installs the application. Runs as root in the build VM |
| `check-app.sh` | Proves it works: after the install, and again before sealing. Fails the build if it fails |
| `seal-app.sh` | Removes per-machine state (generated IDs, keys, accounts, data) before Ubuntu is sealed |
| `icon.png` | Optional icon for GNS3 |

`recipe.json` fields:

| Field | Meaning |
|---|---|
| `label`, `description`, `usage` | Shown in the portal; `usage` goes into the appliance as its Usage text |
| `cpus`, `ram`, `disk_gb` | Defaults for the build VM and the appliance |
| `min_cpus`, `min_ram` | The form refuses less; the builder also refuses a VM bigger than the server's free memory |
| `min_free_gib` | Free space needed in the appliance store and the builder's work folder |
| `cpu_flags` | CPU features the server must have (the build VM uses `-cpu host`), e.g. `["avx", "sse4_2", "aes"]` |
| `requires_license`, `license_url` | The form asks for acceptance, which is audited and passed to the VM as `APPLIANCE_ACCEPT_LICENSE=1` |
| `services` | systemd units stopped before `seal-app.sh` |
| `ports` | Documentation of the guest ports, shown on the recipe page |
| `packages` | Ubuntu packages installed before `install-app.sh` |

Built in: **WebGoat** (pinned release, checksum verified; student data cleared at sealing), **DVWA**
(empty database per clone), **Splunk Enterprise**, and **Custom**.

**Splunk** follows the supplied Splunk kit. The pinned package is downloaded and checked, installed and
**never started** on the golden image. The admin account, GUID and data are created per clone: after a
clone's first boot run `sudo initialize-splunk-lab --accept-license`. It needs 12 GiB of memory, 12 GiB
free, and a CPU with `avx`, `sse4_2` and `aes`.

**Custom** takes extra packages, your setup script, an optional check script and a **required clean-up
script**. A clone that keeps another clone's IDs causes trouble that is hard to trace, so the build will
not start without one. Write `true` if the software keeps no per-machine identity.

### From a link or a package

At the top of **Appliance builder**, paste one of these and click **Analyse**:

| You paste | What the builder service does |
|---|---|
| A `.deb` download link, or the `wget`/`curl` command that fetches it | Downloads it, pins its SHA-256, and reads its name, version, architecture (amd64 only), dependencies, installed size and the systemd services it ships |
| A GitHub repository link | Pins the default branch's current commit and picks, in this order: a `.deb` in the latest release (as above), a `compose.yaml`/`docker-compose.yml` (installs Docker and runs it as a service, with the ports it publishes), a `Dockerfile` (builds and runs it, with its `EXPOSE` ports), or a top-level `install.sh`/`setup.sh`. The repository's licence decides whether acceptance is asked |
| Ubuntu package names | Installs them with apt; their services are checked after the install and stopped before sealing |

The result is a **draft**: requirements (CPUs, memory, disk and free space are estimated from the package
or repository size), the three scripts, and notes on what was found and what to check. Review it, edit
anything, tick that you read the scripts, and **Save application**. It then appears under *Application*
for a normal build. Saved applications can be edited later.

What a source cannot tell, the draft leaves to you:

- **Vendor minimums and CPU features.** A `.deb` does not say that Splunk needs 12 GiB and `avx`. Use the
  vendor's system requirements, or a built-in recipe when one exists (the notes say so).
- **Per-clone clean-up.** The generated `seal-app.sh` does nothing until you add what must differ between
  clones.
- **A real check.** Generated checks test that packages are installed, services run and ports answer.
  An `install.sh` repository gets a placeholder check to replace.

Every build prints what it **measured** in the VM after the check: disk and memory used, listening ports
and running services. Use these to tighten the application's numbers.

### Import an application

**Applications › Import an application** takes a zip that holds either:

- `recipe.json` with the three scripts (and `icon.png`) beside it, or
- a profile from **ubuntu-appliance-generator**, as it generates them (`profiles/<id>.json` whose
  `hooks` and `icon` point at files in the same zip). Its `display_name`, `cpus`, `ram`, `disk_gib`,
  `min_*`, `cpu_flags`, `requires_license`, `services`, `ports` and `usage` are used. Host-side settings
  such as SSH, VNC and preview ports do not apply here.

All three scripts are required. Imported recipes are stored in `<builder dir>/recipes`, can be read under
**Scripts** before a build, and can be deleted. Built-in names cannot be replaced. Each build keeps a copy
of the scripts it ran, so editing or deleting a recipe later does not change a build already queued.

## Safety and limits

- The script runs as root **inside the throw-away VM**, with internet access through the server, never on
  the server. Only Platform Admins can start builds.
- A disk with the same name in the store is never replaced; choose another name or version.
- Import and delete are Platform Admin only and audited; imported scripts run as root in the build VM.
- One build at a time; others wait. A build can be cancelled; a service restart fails a running build.
- Everything the VM installs comes from the internet at build time. The image keeps whatever
  was installed. Splunk is the exception by design: its account and identity are created per clone.
- QEMU appliances for x86_64 only.

## Troubleshooting

- *Job stays "queued"*: `systemctl status gns3-lab-portal-builder`; `journalctl -u gns3-lab-portal-builder`.
- *"The build VM stopped before SSH came up"*: `/dev/kvm` is missing or unusable by the GNS3 user (nested
  virtualization off?), or the baseline does not boot.
- *"did not accept SSH in 10 minutes"*: the baseline has cloud-init disabled or a different login user.
- *Install script failed*: the console shows the failing command. Fix the script or recipe and build again.
- *"The application check failed"*: the application installed but did not answer; nothing was published.
- *"needs … CPUs / MiB / GiB free / CPU features"*: the server cannot hold the build now; stop labs, lower
  the size (not below the recipe's minimum), or free disk space.
