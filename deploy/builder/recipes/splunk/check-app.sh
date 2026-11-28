# shellcheck shell=bash
set -euo pipefail
[[ $(dpkg-query -W -f='${Status}' splunk) == 'install ok installed' ]]
[[ $(dpkg-query -W -f='${Version}' splunk) == 10.6.2.5 ]]
[[ -x /opt/splunk/bin/splunk && -x /usr/local/sbin/initialize-splunk-lab ]]
[[ -f /var/lib/gns3-splunk/uninitialized ]]
[[ ! -e /opt/splunk/etc/instance.cfg && ! -e /opt/splunk/etc/passwd ]]
[[ ! -e /opt/splunk/etc/system/local/user-seed.conf ]]
if pgrep -x splunkd >/dev/null; then echo 'Splunk must never be started on the golden build.' >&2; exit 1; fi
echo 'PASS: pinned Splunk package is installed, uninitialized and contains no seeded administrator.'
echo 'This is a package-state check, not a Splunk Web/runtime check. Initialize each published clone separately.'
