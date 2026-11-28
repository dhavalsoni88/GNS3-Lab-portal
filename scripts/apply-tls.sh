#!/usr/bin/env bash
# Reload Nginx so it picks up a new portal certificate.
#
# Installed as /usr/local/libexec/gns3-lab-portal-apply-tls by scripts/setup-https.sh and run as root
# by gns3-lab-portal-tls.service whenever the portal asks for it (the portal writes <tls dir>/reload-request).
# The portal is not trusted with more than that request: this script only reads a short token from it,
# tests the Nginx configuration, reloads Nginx and reports the result in /run. It never copies or writes
# anything the portal controls.
set -uo pipefail

TLS_DIR=${1:?usage: apply-tls TLS_DIR}
STATUS=/run/gns3-lab-portal-tls.status
request="$TLS_DIR/reload-request"

token=""
if [[ -f $request && ! -L $request ]]; then
  token=$(head -c 64 "$request" | head -n 1 | tr -cd 'a-f0-9')
fi
token=${token:-manual}

if output=$(nginx -t 2>&1); then
  if reload_output=$(systemctl reload nginx 2>&1); then
    result="ok reloaded"
  else
    result="error systemctl reload nginx failed: $(printf '%s' "$reload_output" | tail -n 1 | cut -c1-160)"
  fi
else
  message=$(grep -m1 -E '\[(emerg|alert|crit)\]|error' <<<"$output" || tail -n 1 <<<"$output")
  result="error $(printf '%s' "$message" | tr -d '\n' | cut -c1-200)"
fi

tmp=$(mktemp /run/gns3-lab-portal-tls.XXXXXX)
printf '%s %s\n' "$token" "$result" > "$tmp"
chmod 644 "$tmp"
mv -f "$tmp" "$STATUS"
[[ $result == ok* ]]
