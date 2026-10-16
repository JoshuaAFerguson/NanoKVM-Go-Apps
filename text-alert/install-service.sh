#!/usr/bin/env bash
# post_script: install/refresh the Text Alert systemd service (runs as root).
# Safe to run repeatedly; also invoked by the App if the service is missing.
set -euo pipefail

app_dir="${NANOKVM_APP_DIR:?NANOKVM_APP_DIR not set}"
# The path goes into a unit file: allow only plain path characters (systemd
# expands % and $, and whitespace/newlines would break or inject directives).
if [[ ! "$app_dir" =~ ^/[A-Za-z0-9._/-]+$ ]] || [[ ! -f "$app_dir/textd.py" ]]; then
  echo "refusing to install: unexpected App directory '$app_dir'" >&2
  exit 1
fi

unit=/etc/systemd/system/nanokvm-text-alert.service
tmp="$(mktemp /etc/systemd/system/.nanokvm-text-alert.XXXXXX)"
trap 'rm -f "$tmp"' EXIT
cat > "$tmp" <<UNIT
[Unit]
Description=NanoKVM Text Alert
Wants=network-online.target
After=network-online.target

[Service]
ExecStart=/usr/bin/python3 "${app_dir}/textd.py"
WorkingDirectory=${app_dir}
Nice=19
MemoryMax=32M
Restart=on-failure
RestartSec=30
NoNewPrivileges=yes
PrivateTmp=yes
ProtectHome=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
ProtectSystem=full
ReadWritePaths=/etc/systemd/system /var/lib/nanokvm-apps ${app_dir}
RestrictAddressFamilies=AF_INET AF_UNIX AF_NETLINK
CapabilityBoundingSet=CAP_NET_RAW
TasksMax=32

[Install]
WantedBy=multi-user.target
UNIT
chmod 644 "$tmp"
mv -f "$tmp" "$unit"   # atomic replace
trap - EXIT

install -d -m 700 /var/lib/nanokvm-apps/text-alert
systemctl daemon-reload
systemctl enable nanokvm-text-alert.service
systemctl restart nanokvm-text-alert.service
echo "Text Alert service installed and started."
