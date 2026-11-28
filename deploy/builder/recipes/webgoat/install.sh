# shellcheck shell=bash
# Pinned official release, as in the original WebGoat build kit.
tag=v2026.4; asset=webgoat-2026.4.jar
sha256=846dd1d60692558fcab42627e561db2dd22d49fed1eeac68b6cbea088bf13e00
java=/usr/lib/jvm/java-25-openjdk-amd64/bin/java
[[ -x $java ]] || { echo "Java 25 is missing: this Ubuntu release has no openjdk-25-jre-headless." >&2; exit 1; }
install -d -m 755 /opt/webgoat-lab
curl -fsSL -o /opt/webgoat-lab/webgoat.jar "https://github.com/WebGoat/WebGoat/releases/download/${tag}/${asset}"
echo "${sha256}  /opt/webgoat-lab/webgoat.jar" | sha256sum -c -
getent passwd webgoat >/dev/null || useradd --system --home-dir /var/lib/webgoat --create-home --shell /usr/sbin/nologin webgoat
install -d -o webgoat -g webgoat -m 750 /var/lib/webgoat/data /var/lib/webgoat/tmp
printf 'LAB_WEBGOAT_HOST=webgoat.lab\nLAB_WEBWOLF_HOST=webwolf.lab\n' > /etc/webgoat-lab.env
cat > /etc/systemd/system/webgoat-lab.service <<UNIT
[Unit]
Description=WebGoat and WebWolf training appliance
After=network.target
[Service]
User=webgoat
WorkingDirectory=/var/lib/webgoat
EnvironmentFile=/etc/webgoat-lab.env
ExecStart=${java} -Xms256m -Xmx2048m -Duser.home=/var/lib/webgoat -Djava.io.tmpdir=/var/lib/webgoat/tmp -jar /opt/webgoat-lab/webgoat.jar --server.address=0.0.0.0 --webgoat.host=\${LAB_WEBGOAT_HOST} --webwolf.host=\${LAB_WEBWOLF_HOST} --webgoat.port=8080 --webwolf.port=9090 --webgoat.server.directory=/var/lib/webgoat/data/ --webgoat.user.directory=/var/lib/webgoat/data/ --webwolf.fileserver.location=/var/lib/webgoat/tmp/webwolf-fileserver
Restart=on-failure
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now webgoat-lab.service
for _ in $(seq 1 60); do
  curl -fs http://127.0.0.1:8080/WebGoat/login -o /dev/null && curl -fs http://127.0.0.1:9090/WebWolf/login -o /dev/null && { echo "WebGoat and WebWolf answer."; exit 0; }
  sleep 3
done
echo "WebGoat did not answer within 3 minutes." >&2; exit 1
