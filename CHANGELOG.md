# Changelog

All notable changes to the GNS3 Lab Portal are recorded here, newest first.
Releases up to v8.6 were delivered as update packages; their install steps
are kept under **Upgrade notes**. For upgrading an installation, see
[docs/upgrading.md](docs/upgrading.md).

## v9.0.1 — Unreleased

- `scripts/upgrade.sh` installs requirements with `python -m pip`. A `.venv`
  copied from another folder (common on v8.x servers installed from update
  packages) has a `pip` that installs into the original folder, so the
  database step failed with `No module named 'pymysql'`. It also rebuilds
  `.venv` when its Python no longer runs, for example after an OS upgrade.
- Stopping a lab whose GNS3 project is closed no longer opens it first. A
  closed project runs nothing, and opening it failed when a node's image was
  missing ("GNS3 refused the request (HTTP 409)").
- Show GNS3's own short explanation (for example "The image x.qcow2 is
  missing") instead of only an HTTP code; the full response stays in
  `gns3-api.log`.

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
