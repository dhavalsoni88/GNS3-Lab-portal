# shellcheck shell=bash
# DVWA from its upstream repository (the commit used is recorded in /var/www/html/DVWA-COMMIT).
apt-get install -y apache2 mariadb-server php php-mysqli php-gd libapache2-mod-php git curl
rm -rf /var/www/html/*
git clone --depth 1 https://github.com/digininja/DVWA.git /var/www/html
git -C /var/www/html rev-parse HEAD > /var/www/html/DVWA-COMMIT
cp /var/www/html/config/config.inc.php.dist /var/www/html/config/config.inc.php
db_password=$(tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 24)
systemctl enable --now mariadb
mysql -e "CREATE DATABASE IF NOT EXISTS dvwa; CREATE USER IF NOT EXISTS 'dvwa'@'localhost' IDENTIFIED BY '${db_password}'; GRANT ALL ON dvwa.* TO 'dvwa'@'localhost'; FLUSH PRIVILEGES;"
sed -i "s/'db_user'.*=.*/'db_user'     = 'dvwa';/; s/'db_password'.*=.*/'db_password' = '${db_password}';/" /var/www/html/config/config.inc.php
php_ini=$(ls /etc/php/*/apache2/php.ini | head -n 1)
sed -i 's/^allow_url_include.*/allow_url_include = On/; s/^display_errors.*/display_errors = On/' "$php_ini"
chown -R www-data:www-data /var/www/html
systemctl enable apache2
systemctl restart apache2
curl -fsS http://127.0.0.1/login.php | grep -qi dvwa
echo "DVWA installed."
