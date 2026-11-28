#!/usr/bin/env bash
# Runs inside the build VM as the last step. Makes the disk safe to clone: every clone gets its own
# machine ID, SSH host keys and host name, and the temporary build access is removed. Then powers off.
set -euo pipefail
[[ $EUID -eq 0 && -f /etc/gns3-lab-build ]] || { echo "Refusing to seal: not the marked build VM." >&2; exit 1; }
prefix=${HOST_PREFIX:-ubuntu}
install -d -m 700 /var/lib/gns3-golden
cat > /usr/local/sbin/gns3-firstboot <<FIRSTBOOT
#!/usr/bin/env bash
set -euo pipefail
id=\$(cat /etc/machine-id)
name="${prefix}-\${id:0:12}"
printf '%s\n' "\$name" > /etc/hostname
hostname "\$name"
sed -i '/^127\.0\.1\.1[[:space:]]/d' /etc/hosts
printf '127.0.1.1\t%s\n' "\$name" >> /etc/hosts
ssh-keygen -A
rm -f /var/lib/gns3-golden/pending
FIRSTBOOT
chmod 755 /usr/local/sbin/gns3-firstboot
cat > /etc/systemd/system/gns3-firstboot.service <<'UNIT'
[Unit]
Description=Assign a unique host name and SSH keys to this clone
DefaultDependencies=no
After=local-fs.target
Before=network-pre.target ssh.service ssh.socket shutdown.target
Conflicts=shutdown.target
ConditionPathExists=/var/lib/gns3-golden/pending
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/gns3-firstboot
[Install]
WantedBy=multi-user.target
UNIT
install -d -m 755 /etc/systemd/system/ssh.service.d
printf '[Unit]\nRequires=gns3-firstboot.service\nAfter=gns3-firstboot.service\n' > /etc/systemd/system/ssh.service.d/10-gns3-firstboot.conf
systemctl daemon-reload
systemctl enable gns3-firstboot.service
touch /var/lib/gns3-golden/pending
# Remove the temporary build access (key and passwordless sudo) added through cloud-init.
sed -i '/gns3-lab-builder/d' /home/*/.ssh/authorized_keys /root/.ssh/authorized_keys 2>/dev/null || true
rm -f /etc/sudoers.d/90-cloud-init-users /etc/sudoers.d/90-gns3-builder
if command -v cloud-init >/dev/null; then
  cloud-init clean --logs --machine-id --seed
  touch /etc/cloud/cloud-init.disabled
fi
install -d -m 700 /var/lib/gns3-golden/netplan-original
shopt -s nullglob
for file in /etc/netplan/*.yaml /etc/netplan/*.yml; do mv -- "$file" /var/lib/gns3-golden/netplan-original/; done
cat > /etc/netplan/01-gns3-lab.yaml <<'NETPLAN'
network:
  version: 2
  renderer: networkd
  ethernets:
    labnic:
      match:
        driver: virtio_net
      dhcp4: true
      dhcp-identifier: mac
NETPLAN
chmod 600 /etc/netplan/01-gns3-lab.yaml
netplan generate
rm -f /etc/ssh/ssh_host_*key /etc/ssh/ssh_host_*key.pub
truncate -s 0 /etc/machine-id
rm -f /var/lib/dbus/machine-id
ln -s /etc/machine-id /var/lib/dbus/machine-id
systemctl stop systemd-random-seed.service || true
rm -f /var/lib/systemd/random-seed /var/lib/systemd/network/*.lease
apt-get clean
rm -rf /var/lib/apt/lists/* /tmp/* /root/.bash_history
find /var/log -type f -exec truncate -s 0 {} +
rm -f /etc/gns3-lab-build
sync
echo "GNS3-SEAL-COMPLETE"
systemctl poweroff
