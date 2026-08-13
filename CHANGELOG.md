# Changelog

All notable portal changes are documented here. Versions before v7.6 were
delivered as cumulative update packages; v8.5.0 is the current cumulative
source release.

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
