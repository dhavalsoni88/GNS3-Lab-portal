# Administrator guide

For Platform Admins and the people who run the portal server. Installing is
covered in [installation.md](installation.md), every setting in
[configuration.md](configuration.md), and logs and troubleshooting in
[operations.md](operations.md). This page is the day-to-day checklist.

## After installing

1. Sign in as the first administrator (`PORTAL_USERNAME`) and change its
   password under **My profile**.
2. Create a **second local Platform Admin** for emergencies. Local admins
   still work when the directory or SSO is down, and the portal will not let
   you remove or disable the last one.
3. Connect Active Directory or SSO if you use them, and map groups to roles
   ([authentication.md](authentication.md)).
4. Check the security basics below.

## Security checklist

- [ ] Port 3080 (GNS3 API and web interface) is firewalled. Only the portal
      host and trusted administrators can reach it. Anyone who reaches it
      bypasses the portal.
- [ ] The portal is published only over HTTPS, and `.env` has
      `PORTAL_COOKIE_SECURE=true`.
- [ ] `PORTAL_SECRET_KEY` is a long random value. The portal refuses to
      start otherwise. Store a copy safely: losing it signs everyone out and
      makes stored directory passwords unreadable.
- [ ] `.env` is mode `0640`, group `www-data`. The MariaDB password files in
      `/etc/gns3-lab-portal/` are not readable by other users.
- [ ] `PORTAL_AUTH_ENABLED` is `true` (the default).
- [ ] MariaDB is backed up daily, along with the retained-backup and
      template folders.

## Users and roles

Manage people under **Administration › Users**. Each person can hold
several roles; the [role table](authentication.md#roles) lists what each one
allows. Give the least that does the job: most people need **Lab Editor**.
Use **Lab Operator** for people who only start and stop labs, and
**Viewer** for read-only access.

- **New local user:** set a username (it cannot change later, because it
  identifies lab ownership) and a password of 12+ characters.
- **Leaver:** untick *Enabled*. Their session ends at their next click. Their
  labs keep the owner until you reassign them from the lab's ⋯ menu on
  **Labs**.
- **Directory and SSO users** are created at their first sign-in and get
  roles from group mappings. A mapping change applies at each person's next
  sign-in. Removing someone from the directory group removes the role at
  their next sign-in.
- **View as user** shows the portal as that person sees it, to check what
  they can reach. Everything you do is audited as
  `you impersonating them`. Administration pages are blocked while viewing.
- **Locked out:** after five failed sign-ins, a name is refused from that
  address for 15 minutes. This is in the audit log as `locked`. It clears
  itself.

## Labs, templates and storage

- **Labs without an owner** (made directly in GNS3) show *Needs an owner*.
  Assign one from the lab's ⋯ menu; until then only Lab Managers see them.
- **Lab limit:** each user may own 2 labs by default. Change it per user
  with **Labs allowed** on the Users page. Every lab they own counts, running
  or stopped, and so does a lab still being created from a backup. The limit
  is checked before anything is created, for empty labs, backups, templates
  and uploads alike. Platform Admins and Lab Managers are not limited.
- **Templates for everyone from any lab:** open the lab, then **Backups ›
  Stop nodes and back up**. When the backup is complete, tick it and choose
  **Make template**. A template made by a Platform Admin or Template
  Approver is published at once and appears for every user under **New lab
  › From a template**; anyone else's waits under **Templates** for approval.
  The portal keeps its own copy, so deleting the lab or backup later does not
  affect the template. This needs `GNS3_PROJECTS_DIR` set so the portal can
  read snapshot files ([configuration.md](configuration.md)).
- **Appliances:** **Administration › Appliances** publishes a GNS3 appliance
  to every lab. Disks live in the **appliance store**, GNS3's own QEMU image
  folder (`GNS3_IMAGES_DIR`, by default `/home/gns3/GNS3/images/QEMU`); the
  page always shows the exact path, whether the portal can write to it, and
  the free space.
  1. Choose the appliance's `.gns3a` file (from the GNS3 marketplace or the
     vendor) and click **Check disks**. Nothing is uploaded yet.
  2. For each disk the appliance needs, the next page says one of:
     - **In the store:** ready.
     - **Found elsewhere:** the server already has a disk with that name,
       for example under `/home/gns3/GNS3/images`. It is moved into the store
       when you publish (checked against the checksum first), so you don't
       upload it again. Where the portal looks is set by
       `PORTAL_APPLIANCE_SEARCH_DIRS`.
     - **Missing:** the page shows exactly where the file has to be. Either
       copy it there on the server and click **check again**, or upload it
       from the page. An upload shows a progress bar and goes straight into
       the store, so large disks don't time out.
     - **Does not match:** the store already has a different file with that
       name. It is never replaced; rename or remove it on the server first.
  3. Add **Notes** if you like (default login, first-boot steps). Users see
     them under **Usage** when they configure the appliance in GNS3. The
     appliance file's own `usage` text is filled in for you.
  4. **Publish to all labs.** Moving large disks and registering the
     appliance run in the background with a progress bar. Every lab can use
     the appliance as soon as it finishes; appliances users added themselves
     are never touched.
  - The portal must be able to write to the store folder: on the server run
    `sudo setfacl -R -m u:www-data:rwX /home/gns3/GNS3/images/QEMU`
    ([installation guide](installation.md)).
  - Publishing refuses a template name that exists. QEMU options in an
    appliance file are limited to a short safe list (`-nographic`, `-cpu`,
    `-smp`, `-machine`, `-enable-kvm`, `-rtc`, `-vga`, `-no-acpi`,
    `-no-hpet`, `-global`, `-boot`), because they run on the GNS3 server;
    anything else is refused with a message naming it.
  - QEMU appliances only for now. Only Platform Admins can publish.
- **Templates waiting for approval** appear under **Templates** for Template
  Approvers and Platform Admins. Approving publishes the template to
  everyone.
- **Retained backups:** each user's allowance is set on the Users page
  (default 2). Users free a slot with **New lab › From a backup › Delete
  selected backup**; Lab Managers can delete anyone's.
- **Uploads** are limited by `PORTAL_MAX_UPLOAD_BYTES` (default 50 GiB) for
  backups and `PORTAL_MAX_APPLIANCE_IMAGE_BYTES` (default 512 GiB) for
  appliance disks. The Nginx sample sets no limit of its own.
- **Disk space:** watch the snapshot disk, `RETAINED_BACKUP_DIR` and
  `LAB_TEMPLATE_DIR`. Moving snapshots to a separate disk is covered in
  [storage.md](storage.md).

## Audit and monitoring

- **Audit log** (Platform Admin, Auditor) lists every portal action with
  user, source address, result and request ID. Failed and locked sign-ins
  are there too. The records cannot be changed or deleted; see
  [database.md](database.md#audit).
- Give reviewers or a SIEM the `gns3_portal_auditor` MariaDB account. It
  can only read the audit tables.
- **Server status** shows GNS3 CPU and memory, and disk I/O of the portal
  host.

## Routine maintenance

| How often | Task |
|---|---|
| Daily | MariaDB dump, as in [database.md](database.md#backing-up-the-database), plus copies of the retained-backup and template folders. |
| Weekly | Review the audit log for failed sign-ins and unexpected admin actions. Check free disk space. |
| Monthly | Review who holds Platform Admin and Lab Manager. Disable unused accounts. Test restoring the MariaDB dump on a spare server. |
| Each release | `git pull`, then `sudo bash scripts/upgrade.sh` ([upgrading.md](upgrading.md)). Not while backups or imports are running. |

## Moving an old install to MariaDB

Servers that ran a version before v9.0 keep their data in SQLite. v9.0 will
not start until the data is moved. `scripts/upgrade.sh` moves it in one step;
the manual, reversible procedure is in
[upgrading.md](upgrading.md#moving-from-sqlite-to-mariadb).
