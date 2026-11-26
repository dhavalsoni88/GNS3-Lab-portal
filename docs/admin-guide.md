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
- **Templates waiting for approval** appear under **Templates** for Template
  Approvers and Platform Admins. Approving publishes the template to
  everyone.
- **Retained backups:** each user's allowance is set on the Users page
  (default 2). Users free a slot with **New lab › From a backup › Delete
  selected backup**; Lab Managers can delete anyone's.
- **Uploads** are limited by `PORTAL_MAX_UPLOAD_BYTES` (default 50 GiB) and
  by Nginx `client_max_body_size`. Change both together.
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
| Each release | Follow [upgrading.md](upgrading.md). Do not restart during running backups or imports. |

## Moving an old install to MariaDB

Servers that ran a version before v9.0 keep their data in SQLite. v9.0 will
not start until the data is moved. The step-by-step, reversible procedure is
in [upgrading.md](upgrading.md#moving-from-sqlite-to-mariadb).
