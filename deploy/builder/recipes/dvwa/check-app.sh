# shellcheck shell=bash
# Runs after the install and again before sealing; a failure stops the build before anything is published.
set -euo pipefail
systemctl is-active --quiet apache2 && systemctl is-active --quiet mariadb
[[ -f /var/www/html/config/config.inc.php && -s /var/www/html/DVWA-COMMIT ]]
curl -fsS http://127.0.0.1/login.php | grep -qi dvwa
echo "PASS: DVWA answers on port 80 (commit $(cat /var/www/html/DVWA-COMMIT))."
