# KVM Status

At-a-glance health of the NanoKVM and the machine it's plugged into. No setup needed.

| Tile | Shows | Turns yellow/red when |
| --- | --- | --- |
| Video | Target resolution and refresh (e.g. `1080p60`) | No video from the target |
| USB | Whether the target has enumerated the KVM's keyboard/mouse | Not connected |
| Wi-Fi | Signal (dBm), SSID, band | Weak signal / disconnected |
| LAN / Tailnet | LAN IP and Tailscale IP | No IP |
| System | CPU use, SoC temperature, load | ≥ 65 °C / ≥ 75 °C |
| Memory | Free RAM, total, swap used | < 25 MB / < 12 MB free |

The footer shows uptime and free disk. The header turns yellow and says `stale` if
sampling stops.

## Stuck video service

A known firmware bug can leave the video service (`kvm_vin`) spinning at ~100% CPU
with no video, which wastes half of the Go's CPU. When KVM Status sees this for 30 s,
the footer turns red. **Tap it twice** to restart the service; the system brings it
back in about 7 s. Before restarting, the App re-checks that video is still absent and
the service is still busy, so a recovered service is never killed.

Logs: `ssh root@<device> cat /kvmcomm/apps/kvm-status/log.txt`
