# Configuration

The portal reads its settings from environment variables. The systemd unit
loads them from `/opt/gns3-lab-portal/.env`; start from
[`.env.example`](../.env.example). Restart the service after any change.

## GNS3

| Setting | Default | Description |
|---|---|---|
| `GNS3_URL` | `http://127.0.0.1:3080` | GNS3 controller API address used by the portal backend. |
| `GNS3_USERNAME`, `GNS3_PASSWORD` | empty | Controller credentials. Leave empty only if the controller has no authentication. |
| `GNS3_WEBUI_URL` | `GNS3_URL` | Address users' browsers use for **Open in GNS3** links, for example a reverse-proxied URL. |
| `GNS3_WEBUI_SERVER_ID` | `1` | Server number in the bundled GNS3 web UI's project links. |
| `GNS3_TIMEOUT_SECONDS` | `3600` | Longest wait for a single controller request, such as creating a snapshot. |
| `GNS3_IMAGES_DIR` | `<GNS3 folder>/images/QEMU` | The **appliance store**: GNS3's QEMU image folder, where published appliance disks go. `<GNS3 folder>` is the parent of `GNS3_PROJECTS_DIR`, or `/home/gns3/GNS3`. The portal service user needs write access. |
| `PORTAL_APPLIANCE_SEARCH_DIRS` | `<GNS3 folder>/images` | Comma-separated folders searched (four levels deep) for disks that already exist on the server. A disk found there is moved into the store when its appliance is published. |
| `PORTAL_MAX_APPLIANCE_IMAGE_BYTES` | `549755813888` (512 GiB) | Largest single appliance disk the portal accepts. |
| `GNS3_PROJECTS_DIR` | empty | Absolute GNS3 projects folder. Enables backup sizes, downloads and creating labs from backups. |
| `GNS3_SNAPSHOTS_DIR` | empty | Separate snapshot root when snapshots live on another disk. See [storage.md](storage.md). |
| `GNS3_MONITOR_COMPUTE_ID` | first compute | Compute whose CPU and memory the Server status page shows. |

## Portal and sign-in

| Setting | Default | Description |
|---|---|---|
| `PORTAL_AUTH_ENABLED` | `true` | Accounts and roles. **Never set `false` outside an isolated test bench**: with `false`, anyone who reaches the portal has full control. |
| `PORTAL_USERNAME`, `PORTAL_PASSWORD` | `portaladmin` / placeholder | First local Platform Admin, created only when the database is new. |
| `PORTAL_SECRET_KEY` | placeholder | Signs session cookies and encrypts stored directory passwords. The portal **refuses to start** unless it is 32+ characters and not a shipped placeholder. Generate one with `openssl rand -hex 32` and keep it stable. |
| `PORTAL_PUBLIC_URL` | empty | Public `https://` address of the portal. Required for SAML single sign-on. |
| `PORTAL_TLS_DIR` | `data/tls` | Folder for the HTTPS certificate and key that Nginx serves ([https.md](https.md)). |
| `PORTAL_TLS_STATUS_FILE` | `/run/gns3-lab-portal-tls.status` | Where the root reload helper reports whether Nginx accepted a new certificate. |
| `PORTAL_COOKIE_SECURE` | `true` when `PORTAL_PUBLIC_URL` is `https://`, else `false` | Send the session cookie only over HTTPS. Set `false` only for plain-HTTP testing. |
| `PORTAL_SESSION_IDLE_SECONDS` | `1800` | Sign out after this long without activity. |
| `PORTAL_SESSION_MAX_SECONDS` | `28800` | Sign out after this long regardless of activity. |
| `PORTAL_TRUSTED_PROXIES` | `127.0.0.1,::1` | Proxies whose `X-Forwarded-For` header is trusted for audit source IPs. |

Active Directory and SAML providers are configured in the portal, under
**Administration › Identity providers**, not in `.env`. See
[authentication.md](authentication.md).

## Database

| Setting | Default | Description |
|---|---|---|
| `PORTAL_DB_BACKEND` | `mariadb` | Only `mariadb` is accepted. Any other value stops start-up with a pointer to the SQLite migration. |
| `AUDIT_DB` | `./data/audit.db` | Location of a pre-v9.0 SQLite database. Read only by the migration script and by a start-up check: while this file exists and MariaDB has no users, the portal refuses to start. |
| `PORTAL_DB_HOST` | `127.0.0.1` | MariaDB host. `localhost` uses the local Unix socket. |
| `PORTAL_DB_PORT` | `3306` | MariaDB TCP port. |
| `PORTAL_DB_SOCKET` | `/run/mysqld/mysqld.sock` | Socket used when the host is `localhost`. |
| `PORTAL_DB_NAME` | `gns3_lab_portal` | Database name. |
| `PORTAL_DB_USER` | `gns3_portal_app` | Runtime account. |
| `PORTAL_DB_PASSWORD_FILE` | empty | File holding the runtime account's password (preferred). |
| `PORTAL_DB_PASSWORD` | empty | Password in the environment, if no file is used. |
| `PORTAL_DB_SSL_CA` | empty | CA bundle; when set, the connection to MariaDB requires TLS. |
| `PORTAL_DB_AUTO_MIGRATE` | `false` | Apply schema migrations at startup. Needs DDL rights, so normally left off. |
| `PORTAL_DB_MIGRATE_USER`, `PORTAL_DB_MIGRATE_PASSWORD_FILE` | empty | Migration account used by `portal_db.py migrate` and the SQLite import script. |

## Storage

| Setting | Default | Description |
|---|---|---|
| `RETAINED_BACKUP_DIR` | `/mnt/HDD_STORE/my-lab-retained-backups` | Archive for backups kept when a lab is deleted. |
| `LAB_TEMPLATE_DIR` | `/mnt/HDD_STORE/my-lab-templates` | Independent copies of published templates. |
| `PORTAL_UPLOAD_DIR` | `./data/uploads` | Temporary space for uploaded backup files. The systemd unit also points `TMPDIR` here, so uploads never fill the root disk. |
| `PORTAL_MAX_UPLOAD_BYTES` | `53687091200` (50 GiB) | Largest backup file users may upload. The Nginx sample sets `client_max_body_size 0`, so this limit is the one that applies. |
| `PORTAL_AVATAR_DIR` | `./data/avatars` | Private profile photos. |

## Backups, logs and monitoring

| Setting | Default | Description |
|---|---|---|
| `SNAPSHOT_ESTIMATED_SECONDS` | `3600` | Typical backup duration, used for the progress estimate. |
| `PORTAL_LOG_DIR` | `./logs` | Folder for `portal.log` and `gns3-api.log`. |
| `PORTAL_LOG_RETENTION_DAYS` | `14` | Days of rotated logs to keep. |
| `PORTAL_LOG_LEVEL` | `INFO` | Python log level. |
| `PORTAL_MONITOR_DISKS` | `sda,sdb` | Block devices (not partitions) shown on Server status. Check names with `lsblk -d`. |
