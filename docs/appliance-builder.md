# Appliance builder

**Administration › Appliance builder** (Platform Admin, permission `appliances.publish`) makes a GNS3
appliance from your Ubuntu baseline: you choose what goes on it, the portal builds the disk (qcow2) and
the `.gns3a` file, shows the build console live, and hands the result to the normal
[appliance publishing](admin-guide.md) page, which registers it for every lab.

## How it works

1. The builder service clones your baseline disk (the baseline is never changed).
2. It boots the clone with QEMU/KVM, using a temporary SSH key delivered by cloud-init.
3. It installs the packages, the chosen application and your own script, as root inside the VM.
4. It seals the image (new machine ID, SSH host keys and host name for every clone; the temporary key and
   passwordless sudo are removed) and powers the VM off.
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

## Applications

Each application is a folder in `deploy/builder/recipes/` with `recipe.json`, an optional `install.sh`
and an optional `icon.png`. To add one, add a folder; no code changes. Included: **WebGoat** (pinned release,
checksum verified), **DVWA**, **Splunk Enterprise** (you supply the `.deb` URL, its SHA-256, an admin
password and accept the licence), and **Custom** (extra Ubuntu packages and your own script).

On the form you can add extra packages, a script, change CPU, memory and disk, upload a PNG icon, and write
notes that users see under *Usage*.

## Safety and limits

- The script runs as root **inside the throw-away VM**, with internet access through the server, never on
  the server. Only Platform Admins can start builds.
- A disk with the same name in the store is never replaced; choose another name or version.
- One build at a time; others wait. A build can be cancelled; a service restart fails a running build.
- Everything the VM installs comes from the internet at build time. The image keeps whatever
  was installed (for Splunk, the admin password you typed is in the image).
- QEMU appliances for x86_64 only.

## Troubleshooting

- *Job stays "queued"*: `systemctl status gns3-lab-portal-builder`; `journalctl -u gns3-lab-portal-builder`.
- *"The build VM stopped before SSH came up"*: `/dev/kvm` is missing or unusable by the GNS3 user (nested
  virtualization off?), or the baseline does not boot.
- *"did not accept SSH in 10 minutes"*: the baseline has cloud-init disabled or a different login user.
- *Install script failed*: the console shows the failing command. Fix the script or recipe and build again.
