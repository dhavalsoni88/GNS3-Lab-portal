# shellcheck shell=bash
# DVWA keeps no per-machine identity. Its database is created per clone with Setup > Create / Reset Database,
# so drop any tables a check may have created and leave the empty database and its local account.
set -euo pipefail
systemctl start mariadb
mysql -e "DROP DATABASE IF EXISTS dvwa; CREATE DATABASE dvwa;"
systemctl stop mariadb
rm -rf /var/lib/php/sessions/*
echo "DVWA reset: each clone creates its own database from Setup."
