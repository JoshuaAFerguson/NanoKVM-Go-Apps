#!/usr/bin/env bash
# post_script: install/refresh the AI Monitor systemd service (runs as root).
# Safe to run repeatedly; also invoked by the App if the service is missing.
set -euo pipefail

app_dir="${NANOKVM_APP_DIR:?NANOKVM_APP_DIR not set}"
# The path goes into a unit file: allow only plain path characters (systemd
# expands % and $, and whitespace/newlines would break or inject directives).
if [[ ! "$app_dir" =~ ^/[A-Za-z0-9._/-]+$ ]] || [[ "$app_dir" == *..* ]] || [[ ! -f "$app_dir/aimond.py" ]]; then
  echo "refusing to install: unexpected App directory '$app_dir'" >&2
  exit 1
fi

unit=/etc/systemd/system/nanokvm-ai-monitor.service
tmp="$(mktemp /etc/systemd/system/.nanokvm-ai-monitor.XXXXXX)"
trap 'rm -f "$tmp"' EXIT
cat > "$tmp" <<UNIT
[Unit]
Description=NanoKVM AI Monitor
Wants=network-online.target
After=network-online.target

[Service]
ExecStart=/usr/bin/python3 "${app_dir}/aimond.py"
WorkingDirectory=${app_dir}
Nice=10
MemoryMax=40M
Restart=on-failure
RestartSec=30
NoNewPrivileges=yes
PrivateTmp=yes
ProtectHome=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
ProtectSystem=strict
# unit dir: only for removing its own unit after the App is uninstalled
ReadWritePaths=/etc/systemd/system /var/lib/nanokvm-apps ${app_dir}
PrivateDevices=yes
ProtectClock=yes
ProtectKernelLogs=yes
RestrictNamespaces=yes
RestrictSUIDSGID=yes
RestrictRealtime=yes
LockPersonality=yes
MemoryDenyWriteExecute=yes
SystemCallArchitectures=native
RestrictAddressFamilies=AF_INET AF_UNIX AF_NETLINK
# SO_BINDTODEVICE needs CAP_NET_RAW on older kernels
CapabilityBoundingSet=CAP_NET_RAW
TasksMax=40

[Install]
WantedBy=multi-user.target
UNIT
chmod 644 "$tmp"
mv -f "$tmp" "$unit"   # atomic replace
trap - EXIT

install -d -m 700 /var/lib/nanokvm-apps/ai-monitor
systemctl daemon-reload
systemctl enable nanokvm-ai-monitor.service
systemctl restart nanokvm-ai-monitor.service
echo "AI Monitor service installed and started."
