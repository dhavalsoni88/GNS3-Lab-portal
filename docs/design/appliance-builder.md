# Appliance builder (design record, not yet implemented)

Goal: from the portal, build a golden Ubuntu-based GNS3 appliance (qcow2 + `.gns3a`
+ icon) with a chosen application (WebGoat, DVWA, Splunk, a custom script) and show
the build console live. Model: `webgoat-ubuntu-gns3-build-kit-v3`.

## Flow (from the kit)
baseline qcow2 -> clone -> cloud-init seed ISO -> QEMU/KVM with SSH port-forward ->
SSH as `labadmin` -> install app script -> seal (clean machine-id, SSH host keys,
cloud-init, logs) -> poweroff -> publish qcow2 + `.gns3a` -> register template and
PNG symbol (`POST /v2/symbols/<id>/raw`). The existing appliance publishing page
(v9.1) already covers store, checksum and template registration.

## Constraints
- Needs `/dev/kvm`, qemu-system-x86, a baseline Ubuntu cloud image and run time of
  10-60 min. The portal process (unprivileged, no KVM) must not do this itself.
- Console: builder writes a log; portal streams it (polling or SSE) in a new page.
- Splunk: download needs a vendor URL + SHA-256 supplied by the admin and an explicit
  licence acceptance; the portal must not bundle or fetch it silently.

## Decisions needed
1. Privilege model: separate `gns3-lab-portal-builder` service running as `gns3`
   (has KVM and the image dir), driven by job files from the portal (recommended), or
   the portal user given KVM access.
2. Catalog scope for the first release: WebGoat + DVWA + custom installer script;
   Splunk afterwards.
3. Baseline image: who supplies/updates the Ubuntu cloud image.

Not testable in the development container (no KVM); the kit's VM paths were only
mock-tested.
