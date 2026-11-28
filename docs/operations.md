# Operations

## Logs

Set a log folder writable by the service account:

```env
PORTAL_LOG_DIR=/var/log/gns3-lab-portal
PORTAL_LOG_RETENTION_DAYS=14
PORTAL_LOG_LEVEL=INFO
```

It contains two files, rotated at midnight:

- `portal.log`: backup and import job milestones, node stop requests and
  polling, sign-in errors from directories and SSO, and unexpected exceptions.
- `gns3-api.log`: every portal-to-GNS3 request, with method, path, status,
  duration and any controller error.

Credentials and authorization headers are never logged. To follow a backup:

```bash
sudo tail -f /var/log/gns3-lab-portal/portal.log /var/log/gns3-lab-portal/gns3-api.log
```

Service start-up errors, such as a pending database migration, go to the
journal: `sudo journalctl -u gns3-lab-portal -n 100`.

## Audit log

Every portal action is recorded with the user, target, source IP, browser,
request ID and result. Failed sign-ins are recorded with a reason, never the
password. People with the Platform Admin or Auditor role can read it under
**Audit log**. Everyone else sees their own actions under **My activity**.
The audit records cannot be changed or deleted, even by a database
administrator, without leaving a trace; see [database.md](database.md).

## Server status

**Server status** (Platform Admin, Lab Manager, Auditor) refreshes every five
seconds. It shows:

- **CPU**, with a short trend.
- **Memory**: amber from 75%, red from 90%.
- **Disk I/O** for the devices in `PORTAL_MONITOR_DISKS`.

CPU and memory come from GNS3. Disk counters come from the portal host's
`/proc/diskstats`, so they describe the GNS3 server only when both run on the
same host.

## Safeguards

- **Firewall port 3080.** Only the portal host and administrators should reach
  the GNS3 API.
- **Keep authentication on.** With `PORTAL_AUTH_ENABLED=false`, anyone who
  can reach the portal can create, restore and delete labs.
- **Use HTTPS.** Publish the portal through a TLS reverse proxy and set
  `PORTAL_COOKIE_SECURE=true`.
- **Keep off-server copies.** Native snapshots live on the GNS3 server. For
  recovery from losing that server, export labs or copy the snapshot and
  template folders to separate storage on a schedule.
- **Run one portal process.** Sessions, backup and import progress, SSO
  requests and failed sign-in counts are kept in the service's memory. Do not
  add `--workers` to the service file: users would be signed out at random.
- **Avoid restarts during long jobs.** Do not restart the portal while a
  backup or a lab import is running; both run inside the service. Only one
  import runs at a time, so they don't compete for disk and controller I/O.
- **Backups stop nodes.** A backup waits up to 60 seconds for the emulated
  nodes to report `stopped` and leaves them off. Cloud and built-in Ethernet
  switch nodes stay on and do not block it. `GNS3_TIMEOUT_SECONDS` limits how
  long the snapshot request itself may take.

## Troubleshooting

### A new HTTPS certificate does not take effect

The portal reports "Nginx is not set up" or "Nginx refused that certificate".
See [https.md](https.md#troubleshooting).

### Portal unavailable: "Too many open files"

The portal uses one bounded connection pool to GNS3, and the systemd unit
raises the open-file limit to 8192. To check the live count:

```bash
pid=$(systemctl show -p MainPID --value gns3-lab-portal)
sudo ls /proc/$pid/fd | wc -l
systemctl show gns3-lab-portal -p LimitNOFILE
```

### "The GNS3 server is not responding"

The portal could not reach `GNS3_URL`. Check the controller is running and
that its credentials in `.env` are right. `gns3-api.log` shows the exact
error.

### Backup sizes show "Unknown"

Set `GNS3_PROJECTS_DIR` and grant `www-data` read access to the projects and
snapshot folders, as described in [storage.md](storage.md).

### Directory sign-in fails

Use **Test the connection** on the provider's page. Certificate errors mean
the CA is missing or the URL's host name does not match the domain
controller's certificate. Details are in `portal.log`.

### "Too many failed sign-ins"

After five failed sign-ins for one name from one address, the portal refuses
that name from that address for 15 minutes. Each attempt is in the audit log
with the reason `locked`. The lock clears itself. Restarting the portal also
clears every lock, but it signs everyone out too.

### The portal will not start

`journalctl -u gns3-lab-portal -n 50` names the cause. The portal stops on
purpose when:

- `PORTAL_SECRET_KEY` is a placeholder or shorter than 32 characters;
- `PORTAL_DB_BACKEND` is not `mariadb`, or MariaDB migrations are pending;
- MariaDB holds no users while an old SQLite `audit.db` exists. Finish the
  [SQLite move](upgrading.md#moving-from-sqlite-to-mariadb) first.

### "Cross-site request refused" on every form

The portal compares the browser's `Origin` header with the `Host` header the
reverse proxy forwards. If the portal is served on a port other than 80 or
443 and the proxy sends `Host $host`, the port is lost and every form is
refused. Use `proxy_set_header Host $http_host;` (the Nginx sample does), or
set `PORTAL_PUBLIC_URL` to the full address including the port.

### Memory shows a percentage only

GNS3 reports memory usage as a percentage without totals; that is expected.
