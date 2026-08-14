# GNS3 Lab Portal

Current cumulative release: **v8.5.9**. See [CHANGELOG.md](CHANGELOG.md) for
the complete development and release history.

## Local users, roles, and lab ownership

This release adds local portal accounts with **admin** and **user** roles.
Set `PORTAL_AUTH_ENABLED=true` before starting the portal. On its first start,
the existing `PORTAL_USERNAME` and `PORTAL_PASSWORD` values create the initial
Administrator account. Afterwards, manage users at **Admin** in the header.

Passwords are stored as salted `scrypt` hashes in the portal SQLite database;
they are never stored in plain text in the database. Keep a long random
`PORTAL_SECRET_KEY` and use HTTPS before exposing the portal outside a trusted
network.

Portal login sessions are stored server-side. By default they expire after 30
minutes without portal activity or after eight hours total, are revoked on
logout and a new login for the same account, and are invalidated whenever the
portal service restarts. Adjust `PORTAL_SESSION_IDLE_SECONDS` and
`PORTAL_SESSION_MAX_SECONDS` in `.env` only if your security policy requires
different values.

Every authenticated user has **My Profile** in the portal header. Username is
intentionally immutable because it identifies lab ownership. Users may add an
optional first name, last name, and email address, and may change their own
password by entering and confirming a new password.

New and restored labs are automatically assigned to the signed-in user. The
Administrator sees all labs; existing labs remain Administrator-only until an
Administrator chooses an owner in the dashboard's Owner column. An
Administrator can also select **View as user**. This shows that user's
dashboard with a persistent banner, and audit entries preserve the real
Administrator identity.

This release controls access through the portal. A direct GNS3 WebUI URL may
still provide the controller's own project visibility, so do not publish the
controller WebUI directly to untrusted users until controller-level isolation
or a portal-controlled proxy is added.

## Sharing a lab

The owner of a lab can open **Share** in the Lab List and grant access to any
enabled portal User or Admin. Shared users can see and manage that lab from
their own Lab List. The original owner remains unchanged; only the owner or an
Administrator can share, revoke access, or delete the lab. Administrators can
also share any lab. Every share and revoke action is written to the audit log.

Sharing uses a username type-ahead input rather than a complete account
dropdown. Start typing the intended username, select the suggestion, then
share. The server validates that the selected account exists and is enabled.

Administrators see ownership tabs in Lab List: **My & unassigned labs** and
**User labs**. This keeps their own work and unassigned projects separate from
other users' assigned labs without changing Administrator permissions.

The login screen is intentionally minimal and is titled **My Lab Portal**.

## Sharing a backup

On a lab page, use **Share** beside an individual backup to give a specific
enabled portal user a single-use lab template. The recipient does not gain
access to the existing lab. Instead, **Create Lab → From Backup** shows the
template under **Lab templates shared with you**, where they can create their
own separate powered-off lab. Once that import succeeds, the recipient's share
is automatically removed; a failed import leaves it available to retry. The
lab owner or an Administrator can revoke the share before it is used.

## Reusable lab templates

The owner or an Administrator can publish a completed backup as a reusable
**Lab Template**. Unlike a one-time backup share, a template remains available
to each permitted recipient and appears in **Create Lab → From Backup → Lab
template catalog**. A lab created from a template is a separate powered-off
project owned fully by its creator; it does not grant access to the template
source lab.

Current access grants are local-user assignments. The database also records a
generic access subject type, so LDAP/Active Directory group assignments can be
added later without redesigning templates. On publication, the portal copies
and verifies the native snapshot under `LAB_TEMPLATE_DIR`. Templates therefore
survive deletion of their source lab and source backup. Unpublishing a template
removes its archived template copy.

## Retained backups on deletion

Set `RETAINED_BACKUP_DIR=/HDD_STORE/my-lab-retained-backups`. When the
user selects **Keep backups in the retained-backup archive** while deleting a
lab, the portal copies each native snapshot to this directory before deleting
the GNS3 project. Retained backups appear in **Create Lab → From Backup** and
can create a new powered-off lab later. The `www-data` service account needs
write access to this mounted directory.

Before deleting a lab, its owner selects exactly which native backups to copy
to the retained archive. Each local user has a limit of **two** retained backup
copies by default, regardless of which labs they came from. An Administrator
can change the individual user's **Max copies** value in Manage Users. Existing
copies are never deleted automatically when an Admin lowers the limit; the
lower limit prevents only additional retained copies.

The portal header is branded **My Lab**; selecting it always returns to the
main dashboard.

## Store native snapshots on the HDD

Native GNS3 backups normally live inside each active project at
`/home/gns3/GNS3/projects/<project-id>/snapshots`. To keep active project and
node-disk I/O on the SSD while storing only snapshots on the HDD, this package
includes `migrate-snapshots-to-hdd-store.sh`. It moves each project's
`snapshots` directory to `/HDD_STORE/gns3-snapshots/<project-id>` and
replaces it with a symlink. GNS3 continues to use the normal project layout,
but its snapshot files are written to the HDD.

Run the migration only during a maintenance window, with GNS3 and the portal
stopped. First verify `/HDD_STORE` is mounted, then run:

```bash
sudo systemctl stop gns3-lab-portal gns3
cd /opt/gns3-lab-portal
sudo bash migrate-snapshots-to-hdd-store.sh /home/gns3/GNS3/projects /HDD_STORE/gns3-snapshots
```

If the GNS3 service has a different unit name, stop that controller service
instead. Add the following to `.env` so the portal can safely read snapshot
sizes, download them, and create labs from them:

```env
GNS3_SNAPSHOTS_DIR=/HDD_STORE/gns3-snapshots
```

The portal service needs read/traverse access to the new snapshot root. Keep
the controller ownership as `gns3:gns3`; grant the portal service account
read-only ACLs:

```bash
sudo setfacl -m u:www-data:x /HDD_STORE
sudo setfacl -R -m u:www-data:rx /HDD_STORE/gns3-snapshots
sudo setfacl -R -d -m u:www-data:rx /HDD_STORE/gns3-snapshots
sudo systemctl start gns3
sudo systemctl start gns3-lab-portal
```

Snapshots become slower to create because they are written to the HDD, while
the running labs continue to benefit from the SSD. The retained-backup archive
uses `/HDD_STORE/my-lab-retained-backups` and is unaffected.

The dashboard omits the controller address, the former “Your labs” heading,
and the standalone Open GNS3 WebUI button.

## Import progress storage units

While a backup is copied into GNS3 to create a new lab, the progress message
uses automatic binary units (MiB, GiB, or TiB) instead of always using MiB.

A lightweight FastAPI portal for a GNS3 bare-metal controller. It creates, lists, renames, and exports labs; it also creates, restores, downloads, deletes, and clones native GNS3 snapshots. All controller requests are made by the portal backend; browsers never call port 3080 directly.

## Install from GitHub on Ubuntu

This guide installs the current portal source from this repository on an Ubuntu
server. The simplest deployment is to run the portal on the **same server as
the GNS3 controller**, which is the layout used in the examples below. It can
also run on a separate Ubuntu server, but snapshot/download/import features
then require secure read access to the GNS3 project and snapshot storage.

### Before you begin

- GNS3 Server must already be installed, running, and reachable from the
  portal host.
- Use a supported Ubuntu release with Python 3, systemd, and a user allowed to
  run `sudo`.
- Do not expose the GNS3 controller API on port `3080` to ordinary portal
  users. The portal is the user-facing application.
- The portal service runs as `www-data`; it needs write access to portal data
  and logs, and read/traverse access to native GNS3 backups.

### 1. Download the repository

Run these commands on the Ubuntu server that will run the portal. For the
current deployment, that can be the GNS3 server at `192.168.10.10`.

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip acl

sudo git clone https://github.com/dhavalsoni88/GNS3-Lab-portal.git \
  /opt/gns3-lab-portal

cd /opt/gns3-lab-portal
```

If the destination already exists because this is an update, do not clone
again. See [Update an existing installation](#update-an-existing-installation).

### 2. Create the Python environment

```bash
cd /opt/gns3-lab-portal
sudo python3 -m venv .venv
sudo /opt/gns3-lab-portal/.venv/bin/pip install -r requirements.txt
sudo cp .env.example .env
sudo nano /opt/gns3-lab-portal/.env
```

### 3. Configure `.env`

For a portal on the same host as GNS3, use the local controller address for
backend API traffic and the LAN address for the browser's **Access Lab** links:

```env
GNS3_URL=http://127.0.0.1:3080
GNS3_WEBUI_URL=http://192.168.10.10:3080
GNS3_USERNAME=gns3
GNS3_PASSWORD=replace-with-your-gns3-password

PORTAL_AUTH_ENABLED=true
PORTAL_USERNAME=portaladmin
PORTAL_PASSWORD=choose-a-strong-initial-admin-password
PORTAL_SECRET_KEY=replace-with-a-long-random-secret

AUDIT_DB=./data/audit.db
GNS3_PROJECTS_DIR=/home/gns3/GNS3/projects
GNS3_SNAPSHOTS_DIR=/HDD_STORE/gns3-snapshots
RETAINED_BACKUP_DIR=/HDD_STORE/my-lab-retained-backups
LAB_TEMPLATE_DIR=/HDD_STORE/my-lab-templates

PORTAL_LOG_DIR=/var/log/gns3-lab-portal
PORTAL_LOG_RETENTION_DAYS=30
PORTAL_LOG_LEVEL=INFO

GNS3_TIMEOUT_SECONDS=14400
SNAPSHOT_ESTIMATED_SECONDS=3600
```

Generate a suitable value for `PORTAL_SECRET_KEY` and paste it into `.env`:

```bash
openssl rand -hex 32
```

The configured `PORTAL_USERNAME` and `PORTAL_PASSWORD` create the initial
Administrator only when the portal user database is first created. Afterwards,
reset passwords through **Admin → Manage users and access**; changing the
environment password does not change an existing account.

### 4. Create portal data and log directories

```bash
sudo install -d -o www-data -g www-data -m 0750 \
  /opt/gns3-lab-portal/data \
  /opt/gns3-lab-portal/data/uploads \
  /var/log/gns3-lab-portal \
  /HDD_STORE/my-lab-retained-backups \
  /HDD_STORE/my-lab-templates
```

If your HDD is mounted somewhere else, replace `/HDD_STORE` consistently in
both `.env` and these commands. Verify the actual mount with `findmnt` before
using it; do not use an unmounted directory as backup storage.

### 5. Grant GNS3 and the portal access to snapshots

The portal must read native snapshots to display their sizes, download them,
or create a new lab from a backup. Keep files owned by `gns3`; grant the
portal's `www-data` account only the access it needs:

```bash
sudo setfacl -m u:www-data:--x /home/gns3
sudo setfacl -m u:www-data:rx /home/gns3/GNS3
sudo setfacl -R -m u:www-data:rX /home/gns3/GNS3/projects

sudo setfacl -m u:www-data:--x /HDD_STORE
sudo setfacl -R -m u:www-data:rX /HDD_STORE/gns3-snapshots
sudo find /HDD_STORE/gns3-snapshots -type d \
  -exec setfacl -m d:u:www-data:rx {} +
```

#### Automatic HDD links for new portal labs (v8.5.3)

When `GNS3_PROJECTS_DIR` and `GNS3_SNAPSHOTS_DIR` are set, the portal can link
an **empty, newly created or imported** lab's `snapshots` directory directly
to `/HDD_STORE/gns3-snapshots/<project-id>`. This eliminates the need to run
the migration script for future portal-created labs. The portal never moves an
existing non-empty directory.

Enable the narrowly scoped write access required for that operation and make
sure GNS3 can write to HDD folders created by the portal:

```bash
sudo setfacl -m u:www-data:rwx,m::rwx /home/gns3/GNS3/projects
sudo setfacl -m d:u:www-data:rwx,d:m::rwx /home/gns3/GNS3/projects

sudo setfacl -m u:www-data:rwx,u:gns3:rwx,m::rwx /HDD_STORE/gns3-snapshots
sudo setfacl -m d:u:www-data:rwx,d:u:gns3:rwx,d:m::rwx \
  /HDD_STORE/gns3-snapshots
```

This applies only to labs created through the portal. Continue to use the
migration script, while GNS3 is stopped, for pre-existing labs or labs created
directly in GNS3.

To confirm that the portal service account can read snapshots:

```bash
sudo -u www-data find /HDD_STORE/gns3-snapshots \
  -type f -name '*.gns3snapshot' -printf '%s  %p\n' | head
```

### 6. Install and start the portal service

```bash
sudo install -m 644 \
  /opt/gns3-lab-portal/gns3-lab-portal.service \
  /etc/systemd/system/gns3-lab-portal.service

sudo systemctl daemon-reload
sudo systemctl enable --now gns3-lab-portal
sudo systemctl status gns3-lab-portal --no-pager
```

The supplied service binds Uvicorn to `127.0.0.1:8088`. It is intended to sit
behind an existing Nginx/Caddy reverse proxy, which publishes the portal URL
such as `http://192.168.10.10`. If no reverse proxy exists yet, configure one
before allowing users to access the portal. Use HTTPS and set
`https_only=True` in `app.py` before publishing it outside a trusted network.

### 7. Verify the installation

```bash
curl -I http://127.0.0.1:8088/login
sudo journalctl -u gns3-lab-portal -n 100 --no-pager
sudo tail -n 100 /var/log/gns3-lab-portal/portal.log
```

Sign in at the public portal address using the initial Administrator account,
then immediately set a strong password in the Admin page and create ordinary
user accounts as required.

### Update an existing installation

Before updating, take a backup of `.env` and `data/audit.db`. Do not replace
`.env`: it contains deployment-specific passwords, paths, and settings.

```bash
sudo systemctl stop gns3-lab-portal
sudo cp /opt/gns3-lab-portal/.env /opt/gns3-lab-portal/.env.backup
sudo cp /opt/gns3-lab-portal/data/audit.db /opt/gns3-lab-portal/data/audit.db.backup

sudo git -C /opt/gns3-lab-portal pull --ff-only
sudo /opt/gns3-lab-portal/.venv/bin/pip install -r /opt/gns3-lab-portal/requirements.txt

sudo install -m 644 \
  /opt/gns3-lab-portal/gns3-lab-portal.service \
  /etc/systemd/system/gns3-lab-portal.service
sudo systemctl daemon-reload
sudo systemctl start gns3-lab-portal
```

### Snapshot storage note

The included migration script moves snapshots only for projects that already
exist when it is run. After creating new projects, run the script again during
a maintenance window to move those projects' `snapshots` directories to the
HDD and recreate their symlinks. It never moves whole projects or node disks.

To show native backup disk usage, set `GNS3_PROJECTS_DIR` to the absolute
GNS3 projects root (the directory containing UUID-named project folders). The
**Refresh backups** button then reloads the controller snapshot list and safely
scans only that project's `snapshots/` directory. It shows each matching
backup's size and completion time, ordered oldest to newest. On
a future separate portal VM, mount this directory read-only or leave the setting
blank; snapshot management still works but sizes are not shown.

GNS3's API identifies snapshots by UUID, while its native snapshot files use
the visible backup name followed by `.gns3snapshot`. The portal matches both
layouts, so names containing spaces and parentheses are supported.

## Detailed portal and GNS3 API logs

Set one log directory in `.env` and make it writable by the portal service:

```env
PORTAL_LOG_DIR=/var/log/gns3-lab-portal
PORTAL_LOG_RETENTION_DAYS=14
PORTAL_LOG_LEVEL=INFO
```

```bash
sudo install -d -o www-data -g www-data -m 0750 /var/log/gns3-lab-portal
sudo systemctl restart gns3-lab-portal
```

The directory contains:

- `portal.log`: backup job milestones, each requested node stop, polling state,
  timeouts, and unexpected exceptions.
- `gns3-api.log`: every portal-to-GNS3 API method, endpoint, response status,
  duration, connection error, and controller rejection detail.

The logs rotate at midnight and retain 14 days by default. Credentials and
authorization values are not written to either file. To diagnose a failed
backup, use:

```bash
sudo tail -f /var/log/gns3-lab-portal/portal.log /var/log/gns3-lab-portal/gns3-api.log
```

## Export, share, and create a lab from a backup

- **Download portable lab** generates GNS3's portable `.gns3project` archive.
  This is the preferred archive for sharing a whole lab with another controller.
- **Download** next to a snapshot downloads the native `.gns3snapshot` file for
  off-server storage or controlled sharing.
- **Create Lab** is the single creation entry on the main **Your labs** page.
  Select **Blank lab** for a new empty project, or select **From backup**.
  The backup path then lets you choose an existing server-side lab backup or
  upload a native `.gns3snapshot` or portable `.gns3project` from a computer.
  Both backup paths create a separate lab with fresh IDs, do not change the
  source backup, and leave the new lab powered off.

Snapshot download and cloning require `GNS3_PROJECTS_DIR` to be configured and
readable by the `www-data` portal service. For this deployment it is:

```env
GNS3_PROJECTS_DIR=/home/gns3/GNS3/projects
```

The destination controller needs compatible appliance templates and images.
Portable project exports generally do not make proprietary or separately
managed base images portable by themselves.

Start the portal:

```bash
set -a; source .env; set +a
uvicorn app:app --host 0.0.0.0 --port 8088
```

Browse to `http://PORTAL-SERVER-IP:8088`. For internet-facing use, place the portal behind Nginx/Caddy with HTTPS. Change `https_only=False` in `app.py` to `https_only=True` once HTTPS is enabled.

## Run as a service

Copy the project to `/opt/gns3-lab-portal`, then copy `gns3-lab-portal.service` to `/etc/systemd/system/`. Run `sudo systemctl daemon-reload`, `sudo systemctl enable --now gns3-lab-portal`, and use Nginx/Caddy to proxy the public HTTPS address to `127.0.0.1:8088`.

## Portal unavailable: too many open files

This release uses one bounded, reusable HTTP connection pool for controller API
requests and sets the systemd file-descriptor ceiling to 8192. If an earlier
release becomes unavailable with `OSError: [Errno 24] Too many open files`,
install this release and restart the portal. To inspect the live count later:

```bash
pid=$(systemctl show -p MainPID --value gns3-lab-portal)
sudo ls /proc/$pid/fd | wc -l
systemctl show gns3-lab-portal -p LimitNOFILE
```

## Important safeguards

- Firewall GNS3 port `3080` so only the portal host/admin network can reach it.
- With `PORTAL_AUTH_ENABLED=false`, anyone who can reach the portal can create,
  restore, or delete labs and snapshots. Keep it on a trusted internal network
  only. Enable portal authentication or put it behind an identity provider
  before giving other users access.
- Native snapshots are controller snapshots, not off-server disaster recovery copies. Add a scheduled project export/copy to separate storage for recovery from server loss.
- Restore and delete are deliberately confirmed in the UI. The portal writes every action to `data/audit.db`.
- **Create safe backup** runs as a background job: the browser redirects immediately and polls every five seconds while the portal stops every node and GNS3 creates the native snapshot. Refreshing the Backup list rediscovers its active job; it never stops it. The list is ordered oldest to newest and shows an in-progress backup as the newest row with a circular progress ring. Hover its ring to reveal safe cancellation. Before the GNS3 snapshot call, cancellation stops the job. GNS3 has no snapshot-cancel API once it has begun writing, so the portal lets that operation finish safely and then deletes the resulting backup. The portal waits up to 60 seconds for emulated nodes to report `stopped`; it leaves them powered off. GNS3 built-in Cloud and Ethernet switch nodes deliberately remain active and do not block the backup. Do not restart the portal service while a backup is in progress. `GNS3_TIMEOUT_SECONDS` defaults to 3600 seconds and controls the portal-to-controller snapshot request. During snapshot creation the displayed percentage is explicitly an estimate, refreshed every five seconds, because GNS3 does not publish byte-level progress. Set `SNAPSHOT_ESTIMATED_SECONDS` to the usual duration of a backup on this server.
- **Rename lab** uses the GNS3 project API and preserves the project ID, topology, and snapshots.
- **Export / clone backup** handles large archives without keeping them in
  portal memory. Do not restart the portal service while a clone-import is in
  progress. A clone import is intentionally limited to one job at a time to
  avoid competing for disk and controller I/O.

## GNS3 configuration

The supplied default points to `http://192.168.10.10:3080`. If the portal runs on the same GNS3 host, change it to `http://127.0.0.1:3080`, which is preferred.

Each lab page has an **Access Lab** button. It opens that specific project in
the bundled GNS3 WebUI, using
`/static/web-ui/server/<server-id>/project/<project-id>`. The controller shown
by the WebUI uses server ID `1`. Normally no extra setting is needed. When the
portal talks to a local controller address but users access the WebUI through a
public address or reverse proxy, add its user-facing URL to `.env`:

```env
GNS3_WEBUI_URL=https://gns3.example.internal
# Default: 1
GNS3_WEBUI_SERVER_ID=1
```

`GNS3_URL` remains the portal's controller API address; `GNS3_WEBUI_URL` is
only used for browser links.

This MVP uses the existing GNS3 WebUI for topology placement/configuration. The portal owns lab and snapshot management. Embedding a full topology editor is a separate application phase.

## Start and stop a lab

The main dashboard shows labs in a horizontal **Lab List** table, with columns
for Lab No., Name, Manage, Action, and Access Lab. The list is ordered oldest
to newest, so newly created labs are added as the last row. Hover a lab name to
see its GNS3 project ID. Use the pencil beside the name to rename a lab. The
Action column shows **Start lab** only when all emulated nodes are stopped, and
**Stop lab** only while one or more emulated nodes are running. The Access Lab
column opens that specific project directly in GNS3 WebUI. Cloud and built-in
Ethernet switch nodes are left alone because GNS3 reports those as permanently
active. The portal records each action in its audit log and controller API log.
To protect backup consistency, both controls are blocked while that lab has an
active backup.

The rename pencil opens a small edit panel directly below the selected lab
name. The Lab List allows this panel to extend outside its table row.

## Lab page labels

The individual lab page uses **Create lab backup** for native backup creation.
Its final section is titled **Delete this lab** and contains the deletion
confirmation directly, without a repeated inner heading.

## Backup form layout

On each lab page, the backup name field and **Create backup** button appear on
the first row. The safe-backup acknowledgement checkbox appears directly below
them. On narrow screens, the controls wrap into a readable vertical layout.
