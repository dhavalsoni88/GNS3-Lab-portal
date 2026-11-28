# shellcheck shell=bash
set -euo pipefail
# Fail closed if someone initialized the working image. Never delete an existing
# Enterprise GUID/license/account database or run forwarder-only clone cleanup.
[[ -f /var/lib/gns3-splunk/uninitialized ]]
[[ ! -e /opt/splunk/etc/instance.cfg && ! -e /opt/splunk/etc/passwd ]]
[[ ! -e /opt/splunk/etc/system/local/user-seed.conf ]]
if pgrep -x splunkd >/dev/null; then echo 'Splunk is running. Rebuild from the clean baseline before sealing.' >&2; exit 1; fi
if systemctl is-enabled --quiet Splunkd.service 2>/dev/null; then
  echo 'Splunk boot-start is already enabled; this is not an uninitialized golden build.' >&2; exit 1
fi
echo 'Splunk remains uninitialized; each clone will create its own account, GUID and runtime state.'
