# shellcheck shell=bash
set -euo pipefail
systemctl start webgoat-lab.service
for _ in $(seq 1 60); do
  if curl -fs http://127.0.0.1:8080/WebGoat/login -o /dev/null && curl -fs http://127.0.0.1:9090/WebWolf/login -o /dev/null; then
    echo "PASS: WebGoat and WebWolf login pages answer."; exit 0
  fi
  sleep 3
done
echo "WebGoat did not answer within 3 minutes: journalctl -u webgoat-lab" >&2; exit 1
