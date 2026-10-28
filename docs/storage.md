# Storage

GNS3 keeps each project's native snapshots inside the project at
`<projects>/<project-id>/snapshots`. On a server with a fast SSD for running
labs and a larger HDD for backups, the portal can keep snapshots on the HDD
while project files and node disks stay on the SSD.

Examples use `/home/gns3/GNS3/projects` for projects and `/HDD_STORE` for the
HDD mount. Check the HDD is mounted (`findmnt /HDD_STORE`) before using it.

## Settings

```env
GNS3_PROJECTS_DIR=/home/gns3/GNS3/projects
GNS3_SNAPSHOTS_DIR=/HDD_STORE/gns3-snapshots
RETAINED_BACKUP_DIR=/HDD_STORE/my-lab-retained-backups
LAB_TEMPLATE_DIR=/HDD_STORE/my-lab-templates
```

With `GNS3_PROJECTS_DIR` set, the portal shows backup sizes, downloads
backups and creates labs from them. It only ever reads a project's own
`snapshots` folder. With both `GNS3_PROJECTS_DIR` and `GNS3_SNAPSHOTS_DIR`
set, each lab created or imported through the portal gets its `snapshots`
folder linked to `/HDD_STORE/gns3-snapshots/<project-id>` automatically. The
portal never moves a folder that already contains snapshots.

## Moving existing labs' snapshots

For labs that existed before, or were created directly in GNS3, run the
included script during a maintenance window with GNS3 and the portal stopped.
It moves each project's `snapshots` folder to the HDD and leaves a symlink in
its place; GNS3 keeps using the normal project layout.

```bash
sudo systemctl stop gns3-lab-portal gns3
sudo bash /opt/gns3-lab-portal/scripts/migrate-snapshots-to-hdd-store.sh \
  /home/gns3/GNS3/projects /HDD_STORE/gns3-snapshots
sudo systemctl start gns3 gns3-lab-portal
```

Use your controller's service name if it is not `gns3`. Run the script again
later for any projects created outside the portal. It never moves whole
projects or node disks.

Backups become slower to write because they go to the HDD; running labs keep
the SSD's speed.

## Permissions

GNS3 keeps ownership (`gns3:gns3`). The portal's `www-data` account gets only
the access it needs.

Read access to snapshots on the HDD:

```bash
sudo setfacl -m u:www-data:--x /HDD_STORE
sudo setfacl -R -m u:www-data:rX /HDD_STORE/gns3-snapshots
sudo find /HDD_STORE/gns3-snapshots -type d -exec setfacl -m d:u:www-data:rx {} +
```

Write access for automatic linking of new labs:

```bash
sudo setfacl -m u:www-data:rwx,m::rwx /home/gns3/GNS3/projects
sudo setfacl -m d:u:www-data:rwx,d:m::rwx /home/gns3/GNS3/projects
sudo setfacl -m u:www-data:rwx,u:gns3:rwx,m::rwx /HDD_STORE/gns3-snapshots
sudo setfacl -m d:u:www-data:rwx,d:u:gns3:rwx,d:m::rwx /HDD_STORE/gns3-snapshots
```

Check the portal can read snapshots:

```bash
sudo -u www-data find /HDD_STORE/gns3-snapshots -type f -name '*.gns3snapshot' -printf '%s  %p\n' | head
```

## How backups are matched

GNS3's API identifies snapshots by UUID, while snapshot files are named after
the backup followed by `.gns3snapshot`. The portal matches both, so names
with spaces and parentheses work. If it cannot read the projects folder,
backups still work, but sizes show as *Unknown* and downloads are not
available.

## Portable exports

**Settings › Export** on a lab page downloads GNS3's portable `.gns3project`
archive, which is the best format for moving a whole lab to another
controller. The destination needs the same appliance templates and images;
an export does not make separately licensed base images portable.
