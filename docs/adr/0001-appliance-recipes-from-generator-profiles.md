# ADR-0001: Build appliances from requirement-aware profiles with install, check and seal hooks

**Status:** Proposed
**Date:** 2026-11-28
**Deciders:** Portal owner (Platform Admin for the `ironman` lab)

## Context

The portal's appliance builder (v9.3.0) builds an Ubuntu-based GNS3 appliance from a
*recipe*: `deploy/builder/recipes/<id>/recipe.json` plus one `install.sh`. The builder
service (`scripts/appliance-builder.py`, user `gns3`, KVM) clones the baseline, boots it,
runs packages, `install.sh` and the admin's script over SSH, runs one generic
`deploy/builder/seal.sh`, and publishes the qcow2 and `.gns3a`.

Two reference kits were supplied:

- **ubuntu-appliance-generator v1** produces a standalone build kit per application from a
  JSON *profile* or an interactive prompt.
- **splunk-ubuntu-gns3-build-kit v1** is one kit it generated.

What the kits do that the portal does not do yet:

| Capability | Generator kit | Portal builder today |
|---|---|---|
| Requirements | `min_cpus`, `min_ram`, `min_free_gib`, `cpu_flags` (e.g. `avx sse4_2 aes`) checked before and inside the build | Only form limits (1–16 CPU, 512–65536 MiB, 8–512 GiB); nothing checks the host |
| Licence | `requires_license`; install refuses without explicit acceptance | Splunk recipe has a checkbox; other recipes have no flag |
| Hook contract | `install-app.sh`, `check-app.sh`, `seal-app.sh`; install is followed by check, and seal runs check, stops declared `services`, runs seal-app, then generalizes Ubuntu | One `install.sh`; no check step; one seal for every application |
| Unknown applications | Custom kits get a **blocking** `seal-app.sh` (exit 1) until someone writes the clean-up | Custom recipe seals with generic Ubuntu clean-up only |
| Package source | `apt` names, or `deb` (HTTPS URL + publisher SHA-256), or `custom` | `apt` list plus free script |
| Guest identity | Marker `/etc/gns3-appliance.json`, clone UUID marker, `rearm-baseline.sh` | Build marker `/etc/gns3-lab-build` only |
| Splunk | Installed, **never started** on the golden disk; each clone runs `initialize-splunk-lab --accept-license`, choosing its own admin; no GUID, password or index data in the image | Admin password typed in the form and baked into the image; Splunk started during the build, then `clone-prep-clear-config` |
| Run model | Interactive CLI: typed confirmations, the baseline user's password prompted at run time, fixed SSH and preview ports | Unattended service, temporary cloud-init SSH key, live console in the browser |

Forces:

- The goal is to build appliances from a web page, for any application, sized to what the
  application needs. The same must work for the next application without writing code.
- The portal must stay unprivileged. Only the builder service touches KVM and the image folder.
- Some applications are unsafe to clone naively (Splunk GUIDs, licences, accounts). A wrong
  seal produces clones that conflict, and nobody notices until a lab misbehaves.
- The VM paths of both the kits and the portal builder have only been tested with stubs.
  No real KVM build has run yet.

## Decision

**Adopt the generator's profile schema and its three-hook contract as the portal's recipe
format (Option C).** The existing builder service runs it unattended. Recipe folders can come
from the repository or be imported in the portal.

Concretely:

1. **`recipe.json` gains the profile fields.** `min_cpus`, `min_ram`, `min_free_gib`,
   `cpu_flags`, `requires_license`, `services`, `ports` (documentation and usage text),
   and `package {url, sha256, version}` for `deb` installs.
2. **A recipe has `install-app.sh`, `check-app.sh` and `seal-app.sh`.** The builder runs
   install, then check, then stops the declared services, then check, seal-app and the
   Ubuntu seal. A missing or failing check stops the build before anything is published.
3. **Requirements are enforced in three places.**
   - The form: minimum values come from the recipe.
   - The builder, before it boots anything: host free space in the store and work folders,
     host CPU count and available RAM against the VM size, and `cpu_flags` in the host's
     `/proc/cpuinfo` (the VM runs `-cpu host`).
   - The guest, through the hook.
4. **Licences are explicit.** For a recipe with `requires_license`, the form shows the
   vendor's terms link and a required checkbox. The acceptance is audited, and passed to the
   guest as `APPLIANCE_ACCEPT_LICENSE=1`.
5. **Custom recipes are blocked until reviewed.** They get the generator's blocking
   `seal-app.sh`. A Platform Admin must replace it, by editing an imported recipe or adding
   a repository recipe, before a custom build can publish. *Custom (packages and a script)*
   for throw-away experiments stays possible, but it is labelled *unsealed: not for sharing*
   and cannot be published to all labs.
6. **Recipes can be imported.** In the builder, *Import recipe* takes a zip with the profile
   JSON, the three hooks and an optional PNG. It is validated with the generator's rules and
   stored under `<builder dir>/recipes/<id>`, next to the repository's recipes. A built-in
   recipe of the same id wins.
7. **Splunk is rebuilt on the kit's design.** It is installed, never started, and
   initialized per clone with `initialize-splunk-lab`. The admin password field is removed
   from the form.

## Options Considered

### Option A: Keep the current recipe format

| Dimension | Assessment |
|---|---|
| Complexity | Low (nothing to build) |
| Cost | None now; each new application costs a hand-written `install.sh` |
| Scalability | Poor: no requirement checks, so a 12 GiB Splunk VM can start on a host that cannot hold it |
| Team familiarity | High |

**Pros:** no work, no risk to v9.3.0.
**Cons:**
- No check step, so a broken install can be published.
- One seal for every application.
- The Splunk recipe bakes a credential into the image and shares one clone identity.
- Does not meet "create the appliance by fulfilling its requirements".

### Option B: Run the generated kits from the portal

The portal calls `generate.py` for a profile, then the builder service runs the generated
kit's `run.sh provision --seal --publish`.

| Dimension | Assessment |
|---|---|
| Complexity | High: two build runtimes, and the kits are interactive |
| Cost | Medium. Every prompt must be scripted: the typed confirmations, `STOPPED`, and the baseline user's sudo password, which the portal would then have to hold |
| Scalability | Each kit carries its own copy of the runtime, fixed SSH and preview ports, one build per port set |
| Team familiarity | Medium: the kit is known, but the portal does not drive it today |

**Pros:** reuses the kit unchanged, including its own GNS3 registration and icon upload.
**Cons:**
- Duplicates the portal's publishing path, which already checks checksums, never
  overwrites, and audits.
- The portal must store a guest password, where the current design uses a one-time key.
- The console is harder to stream than with the existing service.
- Fixes to the runtime have to go into every generated kit.

### Option C: Adopt the profile schema and hook contract in the portal builder (chosen)

| Dimension | Assessment |
|---|---|
| Complexity | Medium. The schema, three hook calls, requirement checks and an import page reuse `portal_builder.py`, the builder service and its tests |
| Cost | About 1–2 days of work plus the real-KVM test that is owed anyway |
| Scalability | New applications are data (a recipe folder or an import), not code. Host checks stop a build that cannot fit |
| Team familiarity | High: same service, same pages, same tests |

**Pros:**
- One runtime and one publishing path.
- Builds stay unattended with no stored passwords.
- Requirement and licence checks run before any VM starts.
- Unknown applications cannot be published with a naive seal.
- The supplied Splunk profile and hooks drop in almost unchanged.

**Cons:**
- The kit's `rearm-baseline.sh` and clone-UUID marker must be ported.
- Its standalone CLI use (without the portal) is not served by this. The generator stays
  available as a separate tool for that.
- Imported hooks run as root in the build VM, so import must be Platform Admin only, audited,
  and shown in full before the first build.

## Trade-off Analysis

The real choice is **one runtime (C) or two (B)**. B looks cheaper because the kit already
runs, but each of its interactive safeguards is a prompt the portal would have to answer for
a human. The worst is the baseline user's password, which turns a one-time key into a stored
secret. C keeps the portal's unattended model, its checksum-checked publishing that never
overwrites, and its audit trail. It takes over the parts of the kit that carry the safety
knowledge: requirement fields, the check step, per-application seal, the blocking seal for
unknown applications, and Splunk's never-started golden image.

A is only reasonable if no further applications are planned. The request rules that out, and
A also leaves the weaker Splunk recipe in place.

## Consequences

**Easier:**
- Adding an application: write or import a profile and three hooks, with no Python change.
- Sizing: the form refuses less than the application's minimum, and the builder refuses a VM
  the host cannot hold.
- Trust: every published image has passed its own check and seal.

**Harder:**
- Writing a recipe takes more than a single install script. The check and seal hooks must
  actually be written.
- Imported recipes are root code inside the VM. Reviewing them is an administrator duty.

**To revisit:**
- Whether `ports` should create preview forwards during the build, so an admin can open the
  application before sealing. That needs per-build host ports and a link in the console page.
- A gold-image update procedure. The kits refuse to upgrade an existing install, so today
  every version is a fresh build.
- Real-KVM validation of the whole path on `ironman`, with the kit's `VALIDATION.md` as the
  checklist.

## Action Items

1. [ ] Extend `portal_builder.load_recipes` and `make_job` with the profile fields. Validate
   them with the generator's rules, and check that the form's minimums come from the recipe.
2. [ ] Builder service: add the host preflight (free space, CPU count, `MemAvailable`, CPU
   flags). Replace the single install step with install → check → stop services → check →
   seal-app → Ubuntu seal. Keep stub tests for each failure point.
3. [ ] Port the Splunk profile and hooks into `deploy/builder/recipes/splunk/`. Remove the
   admin password input and the started-and-cleared approach.
4. [ ] Give `custom` the blocking seal and the *unsealed: not for sharing* state. Make the
   publish button refuse it.
5. [ ] Add *Import recipe* (zip, Platform Admin, audited, hooks shown before the first build),
   stored in `<builder dir>/recipes`.
6. [ ] Add the licence acceptance to the form and the audit record.
7. [ ] First real build on `ironman`, with WebGoat (no licence), then Splunk. Record the
   results against the kit's `VALIDATION.md`.
