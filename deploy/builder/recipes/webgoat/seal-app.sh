# shellcheck shell=bash
# Clear student accounts, lesson progress and uploads so every clone starts empty (as the original WebGoat kit did).
set -euo pipefail
if systemctl is-active --quiet webgoat-lab.service; then echo "WebGoat is still running; refusing data reset." >&2; exit 1; fi
for directory in /var/lib/webgoat/data /var/lib/webgoat/tmp; do
  [[ -d $directory && ! -L $directory ]] || { echo "Unexpected WebGoat directory: $directory" >&2; exit 1; }
  find "$directory" -xdev -mindepth 1 -delete
done
echo "WebGoat training data cleared."
