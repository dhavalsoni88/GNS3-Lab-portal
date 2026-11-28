# HTTPS and certificates

The portal listens on `127.0.0.1:8088`; Nginx terminates HTTPS in front of it.
**Administration › HTTPS** (Platform Admin, permission `tls.manage`) manages the
certificate Nginx shows. The portal never runs as root: it only writes
certificate files under `PORTAL_TLS_DIR` (default `data/tls`), and a small
root-owned helper reloads Nginx.

## One-time setup

```bash
sudo bash scripts/setup-https.sh --fqdn lab.example.com --san "lab,192.168.10.10"
sudo ufw allow 443/tcp        # if you use ufw
```

This creates a first self-signed certificate, installs
`/etc/nginx/conf.d/gns3-lab-portal.conf` (HTTP redirects to HTTPS), installs
`/usr/local/libexec/gns3-lab-portal-apply-tls` and the systemd units
`gns3-lab-portal-tls.path` / `.service`, sets `PORTAL_PUBLIC_URL=https://<fqdn>`
in `.env` (a backup is kept) and restarts the portal.

If another Nginx file already forwards to `127.0.0.1:8088` the script lists it
and stops. Review it, then add `--replace-existing` to move it to a backup
folder. If `nginx -t` rejects the result, everything is put back. Safe to re-run;
it keeps the installed certificate.

## Choosing a certificate

| Option | Use when | Notes |
|---|---|---|
| **Self-signed** | Labs, first step | Choose the FQDN and extra names (SAN): DNS names, `*.example.com`, IP addresses. Installed immediately. Browsers warn until the certificate is trusted. |
| **CSR** | A CA (public or company) signs it | The private key is created on the server and stays there. Download the `.csr`, send it to the CA, then paste or upload the returned certificate with any intermediates. Pending requests are listed until installed or deleted. |
| **Upload** | You already have a certificate and key | PEM (certificate, intermediates after it, key) or `.pfx`/`.p12`. Encrypted keys need their passphrase. The key must match the certificate. Refused when the page was loaded over plain HTTP. |

After install the portal asks the helper to reload Nginx and waits up to 20
seconds for the result. If Nginx rejects the certificate the previous one is
restored automatically and the page says so. **Restore the previous
certificate** swaps it back by hand. Expired certificates and keys that do not match are refused; a missing Subject
Alternative Name or fewer than 30 days of validity produce a warning.

## Troubleshooting

- *"Nginx is not set up to serve it yet"*: run `setup-https.sh` (above).
- *Certificate saved but browser still shows the old one*: check
  `systemctl status gns3-lab-portal-tls.path gns3-lab-portal-tls.service` and
  `cat /run/gns3-lab-portal-tls.status`. The path unit runs the helper when
  `data/tls/reload-request` changes. Run it by hand with
  `sudo /usr/local/libexec/gns3-lab-portal-apply-tls <tls dir>`.
- *Nginx fails with `[::]:80 ... Address family not supported`*: the server has
  no IPv6; the script only enables IPv6 listeners when the kernel has IPv6, but
  other Nginx sites may still listen on `[::]`.
- Files: `data/tls/active/{fullchain,privkey}.pem`, `previous/` (one step
  back), `pending/` (CSRs). Keep the folder out of backups that leave the
  server unencrypted.
