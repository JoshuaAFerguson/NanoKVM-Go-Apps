# VNC Bridge

Control the machine on your KVM from a regular VNC client. The KVM's hardware JPEG
snapshots stream out as VNC frames, and your keyboard and mouse go straight to the
target as USB input. That gives full key up/down and mouse dragging, unlike the
click-only MCP tools.

Useful for tools that speak VNC: [Guacamole](https://guacamole.apache.org/),
[noVNC](https://novnc.com), Remmina, TigerVNC, or homelab dashboards.

## Setup

1. Install from the Store and set **VNC password** (6 to 8 characters).
2. Open the App on the touchscreen. It shows the address to connect to, e.g.
   `100.122.94.93:5900`.
3. Connect with a **Tight-capable** client: TigerVNC, TightVNC, UltraVNC, Remmina,
   noVNC or Guacamole. **macOS Screen Sharing and RealVNC Viewer won't work**, because they
   don't support Tight JPEG, which the bridge uses to pass the KVM's JPEGs through
   without re-encoding them.

| Setting | Default | Notes |
| --- | --- | --- |
| `VNC_BIND` | `tailscale` | `tailscale` (your tailnet only), `lan` (Wi-Fi/LAN IP), `all`, or a specific IPv4 |
| `VNC_PORT` | `5900` | |
| `VNC_QUALITY` | `50` | JPEG quality 10 to 90; lower uses less bandwidth |
| `VNC_MAX_FPS` | `10` | 1 to 30 |
| `VNC_VIEW_ONLY` | `false` | `true` ignores keyboard and mouse |

## Security

Anyone who knows the password can **type into your target machine**, so:

- VNC password auth is a legacy scheme (DES, 8 characters max) and the session itself
  isn't encrypted. Keep the default `VNC_BIND=tailscale` so only devices on your tailnet
  can reach it, and use `lan`/`all` only on a network you trust.
- Five wrong passwords from one address block that address for 10 minutes.
- At most 2 clients can connect at once. **Disconnect all** on the touchscreen drops
  them all.
- Use `VNC_VIEW_ONLY=true` if you only need to watch.

## Performance

No decoding or re-encoding happens on the device: each frame is the hardware JPEG,
wrapped in a VNC message. Frames are fetched only when a client asks, shared between
clients, and an unchanged screen isn't resent for up to 2 s. With no video from the
target, clients see a "No video from target" screen.

The service runs in the background (`nanokvm-vnc-bridge`, capped at 48 MB), so it
keeps working while the App is closed. It removes itself if the App is uninstalled.

Logs: `/kvmcomm/apps/vnc-bridge/service-log.txt`
