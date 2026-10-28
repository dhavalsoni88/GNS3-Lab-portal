# Roadmap

This page tracks the v9.0 programme: what has been delivered on the
development branch and what comes next. The current production release is
v8.6; see the [changelog](../CHANGELOG.md) for shipped changes.

## Phases

| Phase | Scope | Status |
|---|---|---|
| 1 | SQLite/MariaDB database layer, versioned migrations, append-only audit and change history, MariaDB server audit, Argon2id, verified SQLite-to-MariaDB migration | Done |
| UI | Console redesign: sidebar navigation, labs list with status and filters, tabbed lab page, phone layouts, dark mode | Done |
| 2 | Seven roles enforced on every action; Active Directory over LDAPS with group-to-role mapping; SAML 2.0 SSO (Entra ID, Okta, ADFS); local break-glass administrator | Done |
| 3 | Server registry; one installer script for single-server, controller, compute-join and single-to-multi conversion; lab placement across computes; SNMPv3 health | Next |
| 4 | On-demand AWS GNS3 servers per user or PoC lab, with expiry; lab backup saved before the instance is terminated | Planned |
| Later | Per-user CPU, memory and lab quotas; optional syslog forwarding of audit events; optional separately secured phpMyAdmin link | Planned |

## Decisions for phases 2–4 (agreed 28 Oct 2026)

- **SSO:** SAML 2.0 service provider tested against Microsoft Entra ID, Okta
  and ADFS. Several identity providers can be enabled at once; the sign-in
  page shows only the enabled ones. LDAPS/AD and local accounts remain.
- **AWS instance types:** prefer nested virtualization on 8th-generation
  Intel instances (C8i, M8i, R8i and their flex variants), launched with
  CPU option `NestedVirtualization=enabled`, running KVM. Fall back to a
  bare-metal type (for example `c5.metal`) when nested virtualization is not
  offered in the region or the lab needs it. Offerings are checked at
  request time with `DescribeInstanceTypeOfferings`, not hard-coded.
- **Regions:** `eu-west-1` is the default; a lower-cost alternative region
  (initially `us-east-1`) is offered, with hourly prices read from the AWS
  Pricing API.
- **Networking:** each AWS server joins the main portal cluster as an extra
  GNS3 compute over a site-to-site VPN or Direct Connect; no GNS3 API is
  exposed to the internet.
- **Lifecycle:** when a lab is deleted or expires, the portal exports a
  backup to portal storage first, then terminates the instance.

## Security principles

- The GNS3 controller API is never exposed to ordinary users; the portal's
  backend makes every controller call.
- Directory connections use LDAPS only, with certificate and host-name
  validation; untrusted, expired or mismatched certificates fail closed.
- Directory passwords go only to the directory. The service-account password
  is stored encrypted. No password, bind secret, MariaDB password or session
  token appears in logs, audit records or error pages.
- Sessions are opaque, server-side and stored as hashes.
- A local Platform Admin can always administer the portal while every
  directory and SSO provider is unavailable.
- Infrastructure monitoring of remote servers will use SNMPv3, never
  v1/v2c community strings, and will be visible only to roles with server
  access.
- A test portal must never be connected to production GNS3 servers, as a
  controller URL or as a compute.
- Any database tool such as phpMyAdmin needs its own HTTPS, network and
  account controls, with individual MariaDB accounts and no `root` access. A
  portal link alone does not protect a database.

## Acceptance criteria for v9.0

- Existing v8.x local users can still sign in after migration.
- A group change in the directory or IdP takes effect at the user's next
  sign-in.
- Role permissions are enforced on the server for every lab, backup,
  template, user, audit and server action.
- Migration, sign-in, recovery and multi-server scenarios are tested on a
  separate test VM before the production maintenance window.
