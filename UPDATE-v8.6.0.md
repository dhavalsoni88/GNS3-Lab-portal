# v8.6.1 — Administrator server monitoring fix

This fixes compute discovery. The portal now uses GNS3's registered compute ID
instead of the WebUI route number `server/1`.

This cumulative update adds a **Server status** panel to the dashboard for an
Administrator who is not using View as user. It is not sent to normal users.

The panel refreshes every five seconds and shows CPU usage with a short trend,
memory use/total, and aggregate I/O counters for configured block devices.
Memory turns yellow at 75% and red at 90%.

Add this optional setting to `/opt/gns3-lab-portal/.env` before restarting:

```env
PORTAL_MONITOR_DISKS=sda,sdb
```

Use actual Linux block-device names, not partitions such as `sda1`. Verify
them with `lsblk -d -o NAME,SIZE,MODEL`.

Install the package over v8.5.9, preserve the existing `.env` and `data/`
directory, then restart `gns3-lab-portal` and hard-refresh the browser.
