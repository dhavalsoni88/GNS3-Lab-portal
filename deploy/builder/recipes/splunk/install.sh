# shellcheck shell=bash
[[ -n ${SPLUNK_ACCEPT_LICENSE:-} ]] || { echo "The Splunk licence must be accepted." >&2; exit 1; }
[[ $SPLUNK_URL == https://* ]] || { echo "The Splunk download URL must start with https://" >&2; exit 1; }
[[ ${#SPLUNK_ADMIN_PASSWORD} -ge 8 ]] || { echo "The Splunk admin password needs 8+ characters." >&2; exit 1; }
curl -fsSL -o /tmp/splunk.deb "$SPLUNK_URL"
echo "${SPLUNK_SHA256}  /tmp/splunk.deb" | sha256sum -c -
dpkg -i /tmp/splunk.deb
rm -f /tmp/splunk.deb
install -d /opt/splunk/etc/system/local
printf '[user_info]\nUSERNAME = admin\nPASSWORD = %s\n' "$SPLUNK_ADMIN_PASSWORD" > /opt/splunk/etc/system/local/user-seed.conf
/opt/splunk/bin/splunk start --accept-license --answer-yes --no-prompt
/opt/splunk/bin/splunk enable boot-start -systemd-managed 1 --accept-license --answer-yes --no-prompt
/opt/splunk/bin/splunk stop
/opt/splunk/bin/splunk clone-prep-clear-config   # every clone gets its own Splunk instance ID
echo "Splunk installed."
