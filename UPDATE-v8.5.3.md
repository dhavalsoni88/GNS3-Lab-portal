# v8.5.3 server update

This incremental package is installed over v8.5.2.

It makes future labs created or imported through the portal use a dedicated
HDD-backed `snapshots` link automatically. Existing snapshot directories are
never moved by the portal.

1. Copy the package files to `/opt/gns3-lab-portal`.
2. Keep these values in `/opt/gns3-lab-portal/.env`:

   ```ini
   GNS3_PROJECTS_DIR=/home/gns3/GNS3/projects
   GNS3_SNAPSHOTS_DIR=/HDD_STORE/gns3-snapshots
   ```

3. Apply the v8.5.3 ACL commands from `README.md`, then restart
   `gns3-lab-portal`.

Create a small test lab through the portal and verify that its `snapshots`
entry is a symlink below `/HDD_STORE/gns3-snapshots`. Do not use the portal to
migrate existing labs; use the migration script while GNS3 is stopped.
