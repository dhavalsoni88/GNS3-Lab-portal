# shellcheck shell=bash
set -euo pipefail
[[ ${APPLIANCE_ACCEPT_LICENSE:-} == 1 ]] || { echo 'Use provision --accept-license after reviewing Splunk terms.' >&2; exit 1; }
[[ ! -e /opt/splunk ]] || { echo '/opt/splunk already exists; refusing upgrade/reuse in a clean golden build.' >&2; exit 1; }
for flag in avx sse4_2 aes; do
  if ! grep -m 1 '^flags[[:space:]]*:' /proc/cpuinfo | grep -qw "$flag"; then
    echo "Required guest CPU flag missing: $flag" >&2; exit 1
  fi
done
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl python3
temporary=$(mktemp -d)
trap 'rm -rf -- "$temporary"' EXIT
curl --fail --location --retry 3 --connect-timeout 30 --max-time 3600 --proto '=https' --tlsv1.2 \
  --output "$temporary/splunk.deb" \
  'https://download.splunk.com/products/splunk/releases/10.6.2.5/linux/splunk-10.6.2.5-b07ce3b48d58-linux-amd64.deb'
printf '%s  %s\n' '0e5c48727b7bdb8676c3e723945b4991a2d7a7f56d5d103f0bd9615f75d461db' "$temporary/splunk.deb" | sha256sum -c -
[[ $(dpkg-deb -f "$temporary/splunk.deb" Package) == splunk ]]
[[ $(dpkg-deb -f "$temporary/splunk.deb" Version) == 10.6.2.5 ]]
[[ $(dpkg-deb -f "$temporary/splunk.deb" Architecture) == amd64 ]]
if getent passwd splunk >/dev/null || getent group splunk >/dev/null; then
  echo 'A splunk user/group already exists. Use a clean application-free baseline.' >&2; exit 1
fi
groupadd --system splunk
useradd --system --gid splunk --home-dir /opt/splunk --no-create-home --shell /bin/bash splunk
dpkg -i "$temporary/splunk.deb"
[[ -d /opt/splunk && ! -L /opt/splunk ]]
python3 - <<'PYLAUNCH'
from pathlib import Path
path = Path('/opt/splunk/etc/splunk-launch.conf')
lines = [line for line in path.read_text().splitlines() if not line.lstrip().startswith('SPLUNK_OS_USER=')]
path.write_text('\n'.join(lines) + '\nSPLUNK_OS_USER=splunk\n')
PYLAUNCH
install -d -o splunk -g splunk -m 750 /opt/splunk/etc/system/local
cat > /opt/splunk/etc/system/local/inputs.conf <<'INPUTS'
[splunktcp://9997]
disabled = 0
INPUTS
chown -R splunk:splunk /opt/splunk
install -d -m 700 /var/lib/gns3-splunk
touch /var/lib/gns3-splunk/uninitialized
sha256sum "$temporary/splunk.deb" | cut -d' ' -f1 > /var/lib/gns3-splunk/package.sha256
dpkg-query -W -f='${Package} ${Version} ${Architecture}\n' splunk > /var/lib/gns3-splunk/package-version.txt
cat > /usr/local/sbin/initialize-splunk-lab <<'PYINIT'
#!/usr/bin/env python3
"""Initialize exactly one published Splunk clone; never used while making gold."""
import argparse
import getpass
import grp
import os
from pathlib import Path
import pwd
import re
import subprocess
import time
import urllib.error
import urllib.request

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--accept-license', action='store_true', required=True,
        help='Accept Splunk software terms for this instance after reviewing them')
    parser.parse_args()
    if os.geteuid() != 0: raise ValueError('Run sudo initialize-splunk-lab --accept-license inside a published clone')
    if Path('/etc/gns3-lab-build').exists() or Path('/var/lib/gns3-golden/pending').exists():
        raise ValueError('Do not initialize the golden build; use a published clone after first boot completes')
    marker = Path('/var/lib/gns3-splunk/uninitialized')
    if not marker.is_file(): raise ValueError('This instance was already initialized or is not kit-managed')
    if Path('/opt/splunk/etc/passwd').exists() or Path('/opt/splunk/etc/instance.cfg').exists():
        raise ValueError('Existing Splunk identity/account state found; inspect it rather than resetting it')
    while True:
        username = input('New Splunk administrator username [admin]: ').strip() or 'admin'
        if re.fullmatch(r'[A-Za-z][A-Za-z0-9_.-]{0,63}', username): break
        print('Use a simple administrator username without whitespace or punctuation beyond ._-')
    while True:
        secret = getpass.getpass('New Splunk admin password (12+ characters): ')
        if len(secret) < 12 or any(ord(c) < 32 or ord(c) == 127 for c in secret):
            print('Use at least 12 characters without control characters.'); continue
        if secret != secret.strip() or secret.endswith('\\'):
            print('Avoid surrounding whitespace or a final backslash so the seed preserves your password exactly.'); continue
        if secret != getpass.getpass('Confirm Splunk admin password: '):
            print('Passwords do not match.'); continue
        break
    seed = Path('/opt/splunk/etc/system/local/user-seed.conf')
    fd = os.open(seed, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
    try:
        os.fchown(fd, pwd.getpwnam('splunk').pw_uid, grp.getgrnam('splunk').gr_gid)
        with os.fdopen(fd,'w') as output:
            output.write('[user_info]\nUSERNAME = '+username+'\nPASSWORD = '+secret+'\n')
        secret = None
        subprocess.run(['/opt/splunk/bin/splunk','enable','boot-start','-systemd-managed','1',
            '-user','splunk','-group','splunk','--accept-license'],check=True)
        subprocess.run(['systemctl','daemon-reload'],check=True)
        subprocess.run(['systemctl','enable','Splunkd.service'],check=True)
        subprocess.run(['systemctl','start','Splunkd.service'],check=True)
        deadline = time.monotonic()+300
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen('http://127.0.0.1:8000/en-US/account/login',timeout=5) as response:
                    if response.status == 200: break
            except (OSError, urllib.error.URLError): pass
            time.sleep(3)
        else: raise ValueError('Splunk Web not ready; inspect journalctl -u Splunkd and /opt/splunk/var/log/splunk')
        if not Path('/opt/splunk/etc/passwd').is_file(): raise ValueError('Splunk did not create its account database')
        marker.unlink()
        print('Splunk initialized. Open http://<clone-IP>:8000 and sign in with the new administrator.')
        print('Management TLS:8089; receiver:9997. Review trial/license settings in Splunk Web.')
    finally:
        seed.unlink(missing_ok=True)

if __name__ == '__main__':
    try: main()
    except (ValueError,OSError,subprocess.CalledProcessError) as error: raise SystemExit('Error: '+str(error))
    except (KeyboardInterrupt,EOFError): raise SystemExit('Cancelled. Inspect instance state before retrying; no automatic credential reset is performed.')
PYINIT
chmod 755 /usr/local/sbin/initialize-splunk-lab
echo 'Splunk installed but deliberately never started. Initialize each published clone with:'
echo 'sudo initialize-splunk-lab --accept-license'
