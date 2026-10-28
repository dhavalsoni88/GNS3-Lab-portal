# Security policy

## Reporting a vulnerability

Please do **not** open a public issue for a security problem. Report it
privately, either with GitHub's **Report a vulnerability** button on this
repository's **Security** tab or by contacting the maintainer directly.

Include:

- the version or commit,
- the affected feature,
- steps to reproduce, and
- the impact you expect.

You will get an acknowledgement within five working days. Please allow time
for a fix before any disclosure.

## Supported versions

| Version | Supported |
|---|---|
| v9.0 (development) | Yes |
| v8.6 | Security fixes |
| Older | No |

## Deployment guidance

- **Keep the controller private.** Firewall the GNS3 API (port 3080) so only
  the portal host can reach it, and do not publish the GNS3 web interface to
  untrusted users.
- **Use HTTPS.** Serve the portal through TLS, set
  `PORTAL_COOKIE_SECURE=true`, and keep `PORTAL_AUTH_ENABLED=true`.
- **Protect the secret key.** Use a long random `PORTAL_SECRET_KEY`. Keep
  `.env` readable only by root and the service group.
- **Keep a break-glass account.** Keep at least one local Platform Admin with
  a strong password, stored offline.
- **Prefer MariaDB in production.** Use it for append-only audit logging, and
  forward `server_audit.log` to your SIEM.
