# v8.5.7 server update

This incremental package is installed over v8.5.6.

Users can now add, replace, or remove their own profile picture from **My
Profile**. The portal accepts JPG, PNG, and WebP files up to 2 MiB and stores
them in the private `PORTAL_AVATAR_DIR` directory.

Copy the package contents to `/opt/gns3-lab-portal`, then ensure the service
account owns the private data directory:

```bash
sudo install -d -o www-data -g www-data -m 0750 \
  /opt/gns3-lab-portal/data/avatars
```

Restart the portal service and hard-refresh the browser.
