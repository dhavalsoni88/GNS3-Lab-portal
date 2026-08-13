# v8.5.2 server update

This incremental package is installed over an existing v8.5.1 portal.

1. Extract the package and copy its files to `/opt/gns3-lab-portal`.
2. In `/opt/gns3-lab-portal/.env`, set these paths to the actual mounted HDD:

   ```ini
   GNS3_PROJECTS_DIR=/home/gns3/GNS3/projects
   GNS3_SNAPSHOTS_DIR=/HDD_STORE/gns3-snapshots
   RETAINED_BACKUP_DIR=/HDD_STORE/my-lab-retained-backups
   LAB_TEMPLATE_DIR=/HDD_STORE/my-lab-templates
   ```

3. Grant the `www-data` portal service account read/traverse access to the
   project and snapshot paths, then restart `gns3-lab-portal`.

The portal stores timestamps as UTC but v8.5.2 renders backup completion and
audit times in the timezone configured in each user's web browser/computer.
No server timezone change is required.
