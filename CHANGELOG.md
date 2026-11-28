# Changelog

All notable changes to the GNS3 Lab Portal are recorded here, newest first.
Releases up to v8.6 were delivered as update packages; their install steps
are kept under **Upgrade notes**. For upgrading an installation, see
[docs/upgrading.md](docs/upgrading.md).

## v9.4.2 — 2026-11-28

### Fixes

- **The builder logs in with the baseline user's password** (as the original kits did), asked for on
  the build form. Ubuntu installs usually tell cloud-init to ignore seed disks, so the one-time SSH key
  never reached the VM and builds stopped at "Permission denied". The password is used once to add a
  one-time key and a temporary sudo rule, which sealing removes. It is passed to the builder in a file
  only it and the portal can read, deleted when the build starts, and never logged or stored with the
  job. The cloud-init seed disk is no longer used.

- **A build no longer fails when the baseline disk is larger than the application's disk size** (for
  example a 32 GiB baseline and DVWA's 16 GiB). The builder used to ask `qemu-img` to shrink the disk,
  which it refuses. Now it only grows a disk; a larger baseline keeps its size, and the console says so.

## v9.4.1 — 2026-11-28

### Fixes

- **Analysing GitHub repositories whose compose file is in a subfolder**, such as OWASP crAPI
  (`deploy/docker/docker-compose.yml`). Compose files up to three folders down are found, test and
  example folders are skipped, and the service runs in that folder so its `.env` applies.
- Ports written as `"${LISTEN_IP:-127.0.0.1}:8888:80"` are read, and such variables are set to `0.0.0.0`
  in the service, so lab machines can reach the application instead of only the VM itself.
- Memory and CPUs are sized from the compose file's container limits (`deploy.resources.limits`).
- The generated check now fails when a published port never opens (it used to wait and pass).

## v9.4.0 — 2026-11-28

Upgrade with `git pull` followed by `sudo bash scripts/upgrade.sh` (it creates the new
`/var/lib/gns3-lab-portal-builder/recipes` folder). Design: [ADR-0001](docs/adr/0001-appliance-recipes-from-generator-profiles.md).

### Changed

- **Applications state what they need.** A recipe declares minimum CPUs and memory, free disk, required
  CPU features (for example Splunk's `avx sse4_2 aes`), licence, and the services to stop. The form
  refuses less than the minimum, and the builder checks the server before copying anything.
- **Install, check, clean up.** Every recipe has `install-app.sh`, `check-app.sh` and `seal-app.sh`. A
  failing check stops the build before anything is published; the check runs again before sealing.
- **Splunk is built the safe way** (from the supplied Splunk kit): installed and never started on the
  golden image; each clone creates its own administrator, GUID and data with
  `sudo initialize-splunk-lab --accept-license`. The admin password field is gone, and so is the
  password that v9.3.0 wrote into the image. Rebuild any Splunk appliance made with v9.3.0.
- **Custom appliances need a clean-up script** before they can be built (`true` if nothing differs per clone).
- **Licence acceptance** is a checkbox on the form for recipes that need one, and is audited.
- WebGoat clears student data and DVWA empties its database when sealing.

### New

- **From a link or a package.** Paste a GitHub repository, a `.deb` download link (a `wget` command
  works too) or Ubuntu package names. The builder service reads the source and writes a draft
  application: requirements estimated from the package or repository, the install, check and clean-up
  scripts (pinned by SHA-256 or commit; GitHub repositories run from a `.deb` release, docker compose, a
  Dockerfile or `install.sh`), and notes on what to review. Review, save, build.
- Imported and generated applications can be **edited** in the portal.
- Every build prints what it **measured** in the VM: disk and memory used, listening ports, running services.
- **Import an application** from a zip: `recipe.json` with its scripts, or an ubuntu-appliance-generator
  profile as generated. Imported recipes can be read (**Scripts**) and deleted; built-in ones cannot be
  replaced. Each build keeps a copy of the scripts it ran.

## v9.3.0 — 2026-11-28

Upgrade with `git pull` followed by `sudo bash scripts/upgrade.sh`, then run
`sudo bash scripts/setup-builder.sh` once to use the builder
([docs/appliance-builder.md](docs/appliance-builder.md)).

### New

- **Appliance builder.** **Administration › Appliance builder** builds a GNS3
  appliance on top of your Ubuntu baseline: choose an application (WebGoat,
  DVWA, Splunk, or custom packages and your own script), CPU, memory, disk,
  icon and notes. A separate service running as the GNS3 user clones the
  baseline, installs the application in a QEMU/KVM VM, seals the image for
  cloning, and writes the qcow2 into GNS3's image folder together with the
  `.gns3a`. The build console streams live in the portal, and one click sends
  the result through the normal appliance review to every lab.
- Applications are folders under `deploy/builder/recipes/`; add one without
  changing code.
- Published appliances can carry an icon (uploaded to GNS3 as a symbol).
- **New look: graphite.** Quiet graphite surfaces in light and dark, no colour accent: the primary action is the ink colour, and colour is used only for status (a small link-state dot) and errors. System fonts, line icons in the sidebar, weights 400 and 500, sentence-case labels, a visible focus ring.
- **Navigation grouped by task:** Workspace, Appliances (Library, Builder), Access (Users, Roles & groups, Identity providers), Server (Status, HTTPS).
- **Labs:** one toolbar for status filter, owner filter and search; each lab shows a dot per node (green when running); *Open* is a secondary button so *New lab* is the one primary action.
- **Appliance builder** is a three-step form with a live summary; the build page shows a terminal-style console beside a step list. **HTTPS** shows the certificate on top and the three methods as tabs. **Appliance library** puts the list first and import and store details beside it.
- **Theme switch:** Light, Dark or System (follows the operating system) from the top bar and the sign-in page; the choice is remembered in the browser.
- **Sign-in page** shows what the product does, with a small topology drawing.
- Not yet run against a real KVM server: the VM steps are tested only with
  stub tools. Please report what the console shows on the first build.

## v9.2.0 — 2026-11-28

Upgrade with `git pull` followed by `sudo bash scripts/upgrade.sh`, then, to
use the new feature, `sudo bash scripts/setup-https.sh --fqdn <your name>`
once ([docs/https.md](docs/https.md)).

### New

- **HTTPS from the portal.** **Administration › HTTPS** shows the current
  certificate (names, issuer, expiry, fingerprint) and can create a
  self-signed certificate for a chosen FQDN and Subject Alternative Names
  (DNS names, wildcards, IP addresses), create a signing request (CSR) whose
  private key never leaves the server, install the certificate a CA returns,
  or upload a certificate and key (PEM or .pfx). Keys are checked against the
  certificate; a private key is refused over plain HTTP. A replaced
  certificate can be put back with one click.
- `scripts/setup-https.sh` installs Nginx for HTTPS (HTTP redirects to HTTPS)
  and a small root-owned helper that reloads Nginx after a new certificate is
  installed, so the portal itself never runs as root. If Nginx rejects a new
  certificate, the previous one is restored automatically.

## v9.1.1 — 2026-11-28

### Fixes

- **Published appliances no longer make GNS3 warn "Could not find '' in the
  Qemu binaries list".** The template now carries the path of the QEMU program
  for its CPU (for example `/usr/bin/qemu-system-x86_64`), taken from the GNS3
  server. The review page shows it, and says so when the server has none.
  An appliance published with v9.1.0 shows the warning once: open its
  template, choose the QEMU binary and click OK, or remove the template and
  publish it again.
- The default template name no longer repeats the version when the
  appliance's name already contains it ("WebGoat … gold-v1 gold-v1").
- **The template name can be edited** on the review page before publishing.

## v9.1.0 — 2026-11-28

Upgrade with `git pull` followed by `sudo bash scripts/upgrade.sh`
([docs/upgrading.md](docs/upgrading.md)). It applies migration
`004_lab_limit` and restarts the portal. To publish appliances, give the
portal write access to GNS3's image folder (see the installation guide); the
script says so if it lacks it.

### New

- **Lab limit per user.** Each user may own 2 labs by default; administrators
  change it per user (**Labs allowed** on the Users page). It is checked
  before any lab is created: empty, from a backup or template, or uploaded.
  Platform Admins and Lab Managers are not limited. Database migration
  `004_lab_limit` adds the column and records limit changes in the change
  history.
- **Publish appliances to every lab** (**Administration › Appliances**,
  Platform Admins). Choose a QEMU `.gns3a` file and the portal shows, per
  disk, whether the server already has it:
  - in the **appliance store**, GNS3's QEMU image folder (`GNS3_IMAGES_DIR`,
    shown on the page together with free space and whether it is writable);
  - **elsewhere on the server** (`PORTAL_APPLIANCE_SEARCH_DIRS`), in which
    case it is checked against the appliance's checksum and moved into the
    store when publishing;
  - or **missing**, with the exact path it must be at. Only then is an
    upload offered. It streams straight into the store with a progress bar
    and no temporary copy, so large disks no longer time out.

  Publishing runs in the background with a progress bar and registers the
  template; optional **Notes** appear under **Usage** in GNS3. Existing
  templates and images are never replaced. QEMU options in an appliance file
  are limited to a safe list, because they run on the GNS3 server.
- **Make template no longer times out.** Copying a large backup into the
  template archive ran inside the web request, so a proxy gave up with
  "504 Gateway Time-out" after a minute. It now runs in the background and
  the lab's Backups tab shows a progress bar; a backup that is already a
  template is refused before any copy starts.
- **Releases are tagged automatically.** When `version` in `pyproject.toml`
  is new on `main`, GitHub creates the `vX.Y.Z` tag and a release from this
  changelog.

### Fixes

- `scripts/upgrade.sh` installs requirements with `python -m pip`. A `.venv`
  copied from another folder (common on v8.x servers installed from update
  packages) has a `pip` that installs into the original folder, so the
  database step failed with `No module named 'pymysql'`. It also rebuilds
  `.venv` when its Python no longer runs, for example after an OS upgrade.
- Stopping a lab whose GNS3 project is closed no longer opens it first. A
  closed project runs nothing, and opening it failed when a node's image was
  missing ("GNS3 refused the request (HTTP 409)"). Stop treats only an
  explicitly closed project as already stopped; an unexpected or malformed
  status is refused without touching the lab.
- GNS3 error details stay out of what users see (as since v9.0.0); the full
  response is in `gns3-api.log` for administrators.
- The lab limit can no longer be bypassed by sending requests at the same
  time, and labs deleted directly in GNS3 no longer count against it.
- The Nginx sample forwards `Host $http_host`, so sign-in works on a
  non-standard port; `Origin: null` and the other unsafe methods (PUT,
  PATCH, DELETE) are covered by the cross-site check.
- `scripts/setup-mariadb.sh` restarts MariaDB only when its audit settings
  changed, instead of on every upgrade. CI runs `shellcheck` on the scripts.
- Background jobs keep a reference to their task; half-copied template
  folders left by a crash are removed at start-up; the Templates page no
  longer asks GNS3 about every lab; an empty "Labs allowed" box no longer
  shows a raw error.

## v9.0.0 — 2026-11-26

Upgrade any server with `git pull` followed by `sudo bash scripts/upgrade.sh`
([docs/upgrading.md](docs/upgrading.md)). SQLite installs move to MariaDB in
that step. Take a server snapshot first.

### Database and audit

- **MariaDB is now the only database.** SQLite support and its schema code
  are removed. The portal refuses to start with `PORTAL_DB_BACKEND=sqlite`,
  or on an empty MariaDB while an old SQLite `audit.db` exists. Move existing
  data with `scripts/migrate-sqlite-to-mariadb.py`
  ([upgrading.md](docs/upgrading.md#moving-from-sqlite-to-mariadb)). The
  script now also copies roles, identity providers and group mappings from
  v9-preview SQLite installs.
- Add `scripts/upgrade.sh`: after `git pull`, one command upgrades any
  server, moving SQLite installs to MariaDB with a verified copy.
- One database layer (`portal_db.py`).
- Add versioned MariaDB migrations, `scripts/setup-mariadb.sh` (least-privilege
  accounts, server audit plugin), and a verified, re-runnable
  SQLite-to-MariaDB data migration.
- Record source IP, user agent, request ID and outcome with every audit event;
  audit failed logins.
- On MariaDB, make audit tables append-only and record a change history (old
  and new values, without password hashes) for users, lab ownership, shares,
  retained backups and templates.
- Hash passwords with Argon2id and upgrade v8.x scrypt hashes at sign-in.
- Add automated tests, run on MariaDB.

### Security and reliability

- Sign-in is on by default (`PORTAL_AUTH_ENABLED` now defaults to `true`).
- Changing your own password asks for the current one. An administrator's
  password reset signs that user out everywhere.
- Refuse form posts from other websites (an `Origin` check), including
  sibling subdomains that the browser's SameSite rule lets through.
- The session cookie is HTTPS-only by default when `PORTAL_PUBLIC_URL` is
  `https://`.
- Refuse `.` and `..` in lab and backup IDs before calling GNS3; a backup ID
  of `..` could turn "delete backup" into "delete lab".
- Users no longer see GNS3's raw error text; it stays in `gns3-api.log`.
- Cap pending SAML sign-ins in memory, and rate-limit sign-in pages in the
  Nginx sample. The sample now also loads on Nginx 1.18 and 1.24 (Ubuntu
  22.04 and 24.04).
- Only the person who started a lab import, or a Lab Manager, can read its
  progress.
- Remove the template "access" list. It never restricted anything: approved
  templates are available to everyone.
- Refuse to start with a placeholder or short `PORTAL_SECRET_KEY`. A known
  key would let a user forge "view as" sessions.
- Lock a name for 15 minutes after five failed sign-ins from one address.
  Unknown names take as long to refuse as wrong passwords.
- End "view as user" as soon as the administrator loses the right to manage
  users.
- Deleting a lab while keeping backups now copies every backup before
  recording any, and undoes them if GNS3 refuses the delete, so a failure
  leaves the lab and the retained-backup quota untouched.
- Users can delete their retained backups to free a slot.
- Limit uploads with `PORTAL_MAX_UPLOAD_BYTES` (default 50 GiB). The service
  buffers uploads in `data/uploads` instead of `/tmp`.
- Refuse restore, backup deletion and lab deletion while a backup is running.
- All lab imports share one path, so every import kind follows the same
  rules (one at a time, name checks, owner checks).
- The labs page makes about ten database queries in total instead of about
  eleven per lab.

### Interface

- Replace the portal layout with the "console" design: a sidebar
  (Labs, New lab, Templates, Activity/Audit log, Server status, Users), a
  labs list with status, node counts, last backup, filters and search, and
  a tabbed lab page (Overview with nodes, Backups, Sharing, Settings).
- Move lab creation to its own page, templates and the approval queue to
  Templates, the audit trail to its own page (with source IP and result),
  and the server monitor to Server status.
- Make every table usable on phones (rows become cards); add dark mode.
- Return to the lab page after starting, stopping, sharing or renaming it.
- Show backup dates when GNS3 reports them as Unix timestamps, or without a
  completion time.
- Show server memory from GNS3's `memory_usage_percent`; it previously read
  "Unavailable".
- Show the error page, or the sign-in page, to browsers instead of raw JSON
  errors.

### Roles and sign-in

- Add seven roles (Platform Admin, Lab Manager, Template Approver, Lab
  Editor, Lab Operator, Viewer, Auditor), enforced on every lab, backup,
  template, user, audit and server-status action. v8.x admins become
  Platform Admins and users become Lab Editors.
- Add Active Directory sign-in over LDAPS only, with CA and hostname
  verification, nested-group support, connection test and group search.
- Add SAML 2.0 single sign-on for Microsoft Entra ID, Okta and ADFS, with
  signed-assertion, request-ID and replay checks.
- Map directory and SSO groups to roles; group roles refresh at each
  sign-in. Keep a local Platform Admin as break-glass access.
- Restrict restoring and deleting backups, and deleting labs, to the lab
  owner or a Lab Manager.

### Deployment and repository

- Publish under the MIT licence.

- Add `PORTAL_COOKIE_SECURE` to send the session cookie only over HTTPS.
- Change the built-in `GNS3_URL` default to `http://127.0.0.1:3080`. Set it
  explicitly in `.env`.
- Move the systemd unit to `deploy/systemd/` and the snapshot migration
  script to `scripts/`; add an Nginx example in `deploy/nginx/`.
- Reorganise documentation into `docs/`, fold the per-release `UPDATE-*.md`
  notes into this changelog, and add contribution, security and CI files.
- Remove the superseded v9.0 planning files (`config/ldap.env.example`,
  draft MariaDB schema) and the roadmap web page, now `docs/roadmap.md`.
- Fix the v8.6.1 dashboard, whose monitoring panel replaced all of the page's
  other scripts.

## v8.6.1 — Monitoring compute discovery fix

- Discover the registered GNS3 compute instead of treating the WebUI server
  route number as a compute ID.

## v8.6.0 — Administrator server monitoring

- Add an Administrator-only CPU and memory monitoring panel with a short CPU trend graph.
- Mark memory use at 75% as warning and 90% as critical.
- Show aggregate read/write counters and current rates for configured local block devices.

Upgrade notes: optionally set `PORTAL_MONITOR_DISKS` to the block devices to
show (for example `sda,sdb`; whole disks, not partitions such as `sda1`;
check with `lsblk -d -o NAME,SIZE,MODEL`).

## v8.5.9 — Administrator-approved templates

- Let every lab owner submit a backup as a template.
- Keep non-Administrator submissions pending and unavailable for import.
- Add an Administrator approval queue; approval publishes a template to every
  portal user.
- Preserve existing templates as approved during the database upgrade.

## v8.5.8 — Server-side session security

- Store only opaque, hashed session tokens in the portal database.
- Expire sessions after 30 minutes idle or eight hours total by default.
- Invalidate all existing sessions after a portal service restart.
- Revoke sessions on logout and when the same user signs in again.

Upgrade notes: every user signs in again after the restart.

## v8.5.7 — Profile photos

- Let each signed-in user upload, replace, or remove a profile photo from My
  Profile.
- Accept validated JPG, PNG, and WebP images up to 2 MiB only.
- Store photos privately and serve them only to the signed-in account.

Upgrade notes: create the private photo directory before restarting:
`sudo install -d -o www-data -g www-data -m 0750 /opt/gns3-lab-portal/data/avatars`.

## v8.5.6 — Dismissible account menu

- Close the account menu when the user clicks or taps outside it.
- Close the account menu when the user presses `Esc`.

## v8.5.5 — Account menu and display name

- Replace header text links with one compact account button containing an
  avatar icon and the signed-in user's display name.
- Use the configured first and last name when available; otherwise use the
  immutable username.
- Place My Profile, Administrator-only Manage Users, and Sign out in the
  expandable account menu.

## v8.5.4 — Bulk backup actions

- Rename the backup timestamp column to **Completed at**.
- Keep Download, Restore, and Share as concise per-backup actions.
- Move template publishing and backup deletion to the section toolbar.
- Support selecting multiple backups for one confirmed bulk-delete operation.
- Require exactly one selected backup to publish a template, pre-fill its name,
  and allow the user to edit that name before publishing.

## v8.5.3 — Automatic HDD storage for new labs

- Link empty snapshot directories for labs created or imported through the
  portal directly to their dedicated `GNS3_SNAPSHOTS_DIR` HDD location.
- Never alter an existing non-empty snapshot directory; legacy labs remain
  safe for separate maintenance-window migration.
- Continue showing size and completion time for safe local snapshot folders,
  so legacy backups remain visible before migration.

Upgrade notes: keep `GNS3_PROJECTS_DIR` and `GNS3_SNAPSHOTS_DIR` set, apply
the write ACLs in [docs/storage.md](docs/storage.md#permissions), then create
a test lab and confirm its `snapshots` entry is a symlink into the snapshot
root. Migrate existing labs with the script while GNS3 is stopped.

## v8.5.2 — Backup layout, size scan, and local time

- Tighten the Lab Backup action layout and permit horizontal scrolling on small
  displays rather than collapsing action controls.
- Resolve dedicated-HDD snapshot directories before scanning their contents.
- Render backup completion and audit timestamps in each user's browser-local
  timezone instead of UTC.

Upgrade notes: set `GNS3_PROJECTS_DIR`, `GNS3_SNAPSHOTS_DIR`,
`RETAINED_BACKUP_DIR` and `LAB_TEMPLATE_DIR` to the real mounted paths and
grant `www-data` read access to the project and snapshot folders. No server
time-zone change is needed.

## v8.5.1 — Independent template archive

- Archive and verify a native snapshot under `LAB_TEMPLATE_DIR` when a template
  is published.
- Import templates from their archive, independent of the original lab.
- Allow source labs and source backups to be deleted after template publication.
- Remove the archived template copy when it is unpublished.

## v8.5.0 — Reusable lab templates and retained-backup quotas

- Publish a completed lab backup as a reusable lab template.
- Share templates with local users now through a generic access table prepared
  for future LDAP/AD group assignments.
- Let recipients create independent, fully owned labs from shared templates.
- Let users choose exactly which backups to retain before deleting a lab.
- Default each user to two retained-backup copies; allow an Administrator to
  set an individual limit from 0 to 100.

## v8.4.11 — Profile password and username clarity

- Use New password and Confirm new password for signed-in profile changes.
- Remove the current-password requirement from the authenticated profile flow.
- Display the immutable username as text rather than a disabled-looking input.

## v8.4.10 — User administration layout

- Place **View as user** beside each username.
- Place **Save** alongside the corresponding password-update field.
- Remove the redundant Impersonate column.

## v8.4.9 — Header navigation cleanup

- Hide the signed-in username from the dashboard header.
- Rename the Admin header link to **Manage Users**.
- Remove the duplicate Manage users and access link from the dashboard.

## v8.4.8 — Profile, admin lab tabs, and share lookup

- Add local user **My Profile** fields for optional name and email, along with
  secure self-service password changes.
- Split the Administrator Lab List into **My & unassigned labs** and **User
  labs** tabs.
- Replace full account dropdowns in lab and backup sharing with username
  type-ahead suggestions.

## v8.4.7 — Deployment documentation

- Add a complete GitHub-to-Ubuntu installation guide.
- Document same-server configuration, ACLs, systemd installation, verification,
  updates, and snapshot-storage behavior.

## v8.4.6 — Collapsible audit trail

- Rename **Recent activity** to **Audit trail**.
- Keep audit records collapsed until the user selects the Audit trail control.

## v8.4.5 — Backup-source panel visibility

- Show shared lab templates and retained backups only while **Existing lab
  backup** is selected.
- Keep the **Upload backup file** view limited to the upload form.

## v8.4.4 — Single-use backup templates

- Treat a shared backup as a single-use lab template.
- When the recipient successfully creates a lab, remove only that recipient's
  share for that backup.
- Keep the template share available when the import fails, so it can be
  retried.
- Rename the recipient section to **Lab templates shared with you**.

## v8.4.3 — Controller connection-pool / open-file fix

- Reuse a bounded HTTP connection pool for normal portal-to-GNS3 API calls.
- Close the shared client cleanly when the portal stops.
- Set the portal systemd `LimitNOFILE` to 8192.
- Prevent the `OSError: [Errno 24] Too many open files` outage caused by high
  dashboard/API request churn.

## v8.4.2 — Snapshot HDD mount validation

- Correct the snapshot migration script so a destination *within* a mounted
  filesystem, such as `/HDD_STORE/gns3-snapshots`, is accepted.

## v8.4.1 — Actual HDD path and ACL correction

- Validate the destination mount supplied to the migration script rather than
  a hard-coded `/mnt/HDD_STORE` location.
- Document ACLs for `/HDD_STORE` and the portal service account.

## v8.4 — Native snapshot storage on HDD

- Move only each project `snapshots` directory to the HDD and replace it with
  a symlink; projects, node disks, and active lab I/O remain on SSD.
- Add `migrate-snapshots-to-hdd-store.sh` and HDD snapshot documentation.
- Support `GNS3_SNAPSHOTS_DIR` for safe portal size, download, and import
  access through the snapshot symlinks.

## v8.3 — Backup sharing

- Add **Share backup** alongside existing lab sharing.
- Let a recipient create a separate powered-off lab from one specific backup.
- Keep the recipient isolated from the source lab.
- Remove backup-sharing records when a backup or lab is deleted.

## v8.2 — Lab sharing and login branding

- Rename the login form to **My Lab Portal** and hide the duplicate header.
- Let lab owners and Administrators grant/revoke active-lab access.
- Shared users can operate a shared lab but cannot re-share or delete it.
- Correct Admin impersonation to use the impersonated user's lab scope.

## v8.1.1 — Admin menu authorization

- Show **Admin** and **Manage users and access** only to authenticated Admins
  who are not impersonating another user.

## v8.1 — Retained backups and activity privacy

- Regular users see only their own recent activity; Admins retain global audit
  visibility unless impersonating a user.
- Add delete-with-retained-backups workflow.
- Copy selected native snapshots to `RETAINED_BACKUP_DIR` before project
  deletion and allow later recreation from that archive.

## v8.0 — Local users, roles, ownership, and impersonation

- Add local password-hashed accounts with `admin` and `user` roles.
- Bootstrap the first Admin from existing environment credentials.
- Add login/logout, per-lab ownership, user administration, enable/disable,
  password resets, and last-Admin protection.
- Restrict dashboards and lab operations by owner/shared access.
- Add audited Admin impersonation with a visible banner.

## v7.6.1 — Standalone deployment completion

- Publish a complete standalone package through v7.6.
- Add a safe `.env.example` and log-directory ACL documentation.

## v7.6 — Import size units

- Display backup-import progress in automatic binary units: MiB, GiB, and
  TiB.

## v7.5 — Dashboard and brand cleanup

- Remove the dashboard **Open GNS3 WebUI** button.
- Apply **My Lab** branding through the shared base template.

## v7.4 — Dashboard header cleanup

- Remove the **Your labs** dashboard heading and exposed controller URL.

## v7.3 — My Lab brand

- Rename the portal header/brand to **My Lab**.

## v7.2 — Lab-page wording

- Improve lab-page labels and user-facing action names.

## v7.1.1 / v7.1 — Dashboard lab actions

- Add dashboard start/stop controls and refinements to lab actions.

## v7.0 / v7.0.1 — Backup form layout

- Improve backup form layout and related UI presentation.

## v6.9 — Lab list

- Add the dashboard Lab List with management and navigation actions.

## v6.8 — Legacy GNS3 WebUI access

- Add direct project routes compatible with the controller's legacy WebUI.

## v6.7 / v6.7.1 — Create-lab text and layout

- Clarify Create Lab wording and repair layout/line-break issues.

## v6.6 — Lab start and stop

- Add portal-controlled start and stop actions for all applicable nodes.

## v6.5 — Unified Create Lab flow

- Combine blank-lab creation, server-side backup import, and upload import
  under one Create Lab experience.

## v6.4 — Access Lab

- Add direct **Access Lab** links to a project's GNS3 WebUI route.

## v6.3 — Backup management

- Add native snapshot listing, download, restore, cloning, and delete actions.

## v6.2 — Built-in switch backup handling

- Prevent built-in Cloud and Ethernet-switch nodes from blocking safe backups.

## v6.0 — Detailed logging

- Add rotating portal and controller-API logs with retention settings.
- Add audit logging for portal actions.

## v5.3 — Cloud-node backup fix

- Improve safe-backup handling for Cloud nodes.

## v5.2 / v5.2.1 — Dashboard create/import refinement

- Add dashboard creation/import controls and UI cleanup.

## v5.1 / v5.0 — Human-readable storage and backup progress

- Add automatic storage-size formatting.
- Add background backup progress polling and safe cancellation behavior.

## v4.0 — Portable export and backup cloning

- Add portable project export and clone/import workflows.

## v3.0 — Snapshot size detection

- Add local native-snapshot size detection and matching fixes.

## v2.0 — Safe backup and rename foundation

- Add safe native snapshot creation and GNS3 project renaming.
