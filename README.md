# NanoKVM Go Apps

A personal App Store catalog for the Sipeed **NanoKVM Go** and **NanoKVM Go+**
touchscreens. It uses the same format as the official
[sipeed/NanoKVM-Go-Apps](https://github.com/sipeed/NanoKVM-Go-Apps) `main`
branch, so the device's built-in Store can install directly from it.

## Use as an App Store

1. Open the NanoKVM web UI, then go to **Settings → Apps → Store → Servers**.
2. Click **Add server**, give it a name, and enter this URL:

   ```
   https://github.com/JoshuaAFerguson/NanoKVM-Go-Apps
   ```

3. Switch the Store to that server, pick an App, and click **Install**.
4. On the device touchscreen, open **Apps** and tap the App. To exit, swipe
   inward from the screen edge and confirm.

The device downloads the default branch as
`https://codeload.github.com/JoshuaAFerguson/NanoKVM-Go-Apps/zip/HEAD` and
treats every top-level directory as an App. That means:

- the repo must stay **public**, and `main` **is** the catalog;
- directories starting with `_` (or `.`) are ignored, so tooling lives there;
- everything on `main` ships to the device on every Store browse, so keep it lean.

> App install scripts run as **root** on the device. Only add servers you trust.

## Apps

| App | Description |
| --- | --- |
| [`macro-pad`](macro-pad/) | Touch buttons for key combos, BIOS/boot keys and text on the target. Profiles for Windows, macOS, Linux, mobile, plus your own. Needs the MCP key. |
| [`kvm-status`](kvm-status/) | Dashboard: target video/USB, Wi-Fi, IPs, CPU, temperature, memory. Detects and fixes a stuck video service. |
| [`wol-panel`](wol-panel/) | Wake-on-LAN: tap a machine to wake it, with live online/offline status. |
| [`homelab-glance`](homelab-glance/) | Home Assistant, Uptime Kuma, Proxmox, Prometheus, Kubernetes and Docker status with drill-down. |
| [`screen-watcher`](screen-watcher/) | Background alerts (ntfy, Discord, Pushover, Home Assistant) when the target loses video, blue-screens, goes black or freezes. |
| [`vnc-bridge`](vnc-bridge/) | VNC server for the target (Tight JPEG passthrough, real USB keyboard/mouse). Works with TigerVNC, Remmina, noVNC, Guacamole. |
| [`text-alert`](text-alert/) | **Go+ only.** Notifies when a phrase (e.g. "Build succeeded", "Kernel panic") appears on the target's screen, via on-device OCR. |
| [`media-power`](media-power/) | Mount ISO images on the virtual drive and press the target's power button (Finger Robot) from the touchscreen. Needs the web login. |
| [`hello-nanokvm`](hello-nanokvm/) | Smoke test: confirms this Store installs and runs Apps. |

## Repository layout

```text
.
├── hello-nanokvm/          # one directory per App (the catalog)
│   ├── app.json
│   └── main.py
├── _shared/                # code shared by Apps, copied into each App that uses it
│   ├── kvm_mcp.py          # client for the device's MCP server (keyboard, mouse, screenshot)
│   ├── ui.py               # layout, palette, buttons, headers
│   ├── netinfo.py          # interface IP/netmask via ioctl
│   ├── localtls.py         # pinned TLS for the device's own HTTPS server
│   ├── font.py             # 8x8 font with lowercase + punctuation, fast renderer
│   ├── applog.py           # log.txt in the App dir (launcher discards stdout/stderr)
│   ├── appdata.py          # persistent data in /var/lib/nanokvm-apps (survives updates)
│   ├── notify.py           # ntfy / Discord / Pushover / Home Assistant notifications
│   ├── jpegdc.py           # DC-only JPEG decoder: block-average colours, pure Python
│   ├── vinsnap.py          # snapshot client for the KVM's video service
│   ├── alerter.py          # alert log, pause and retry queue for background services
│   └── servicekit.py       # helpers for Apps with a background systemd service
├── _template/              # starter App copied by _tools/new_app.py
├── _tools/
│   ├── new_app.py          # scaffold a new App with a matching app_id and shared modules
│   ├── sync_shared.py      # copy _shared/ changes into Apps (--check in CI)
│   ├── preview_app.py      # render an App's screen locally to a PPM image
│   ├── validate_catalog.py # check every App against the launcher's rules
│   ├── package_app.py      # build an upload ZIP for Settings → Apps → Upload ZIP
│   ├── fetch_sdk.sh        # pull appbase SDK into _sdk/ for IDE type hints
│   └── tests/
└── .github/workflows/      # CI runs the validator on every push and PR
```

## Developing an App

```bash
./_tools/fetch_sdk.sh                         # once: SDK + guide into _sdk/ (git-ignored)
python3 _tools/new_app.py weather-panel "Weather"
# edit weather-panel/main.py and app.json
python3 _tools/preview_app.py weather-panel /tmp/w.ppm && open /tmp/w.ppm   # local screenshot
python3 -m unittest discover -s _tools/tests
python3 _tools/validate_catalog.py && python3 _tools/sync_shared.py --check
python3 _tools/package_app.py weather-panel   # optional: dist/weather-panel.zip
```

Test quickly without the Store:

```bash
scp -r weather-panel root@<device-ip>:/kvmcomm/apps/   # list refreshes in ~10s
```

### Rules the launcher enforces

- Directory name: lowercase kebab-case, e.g. `weather-panel`.
- `app.json` and `main.py` are both required.
- Required fields: `app_id`, `name`, `creator`, `create_time` (`YYYY-MM-DD`), `version`.
- `app_id` is reverse-domain with ≥ 3 parts, and its last part equals the
  directory name with `-` → `_`. This repo uses
  `io.github.joshuaaferguson.<app_name>`.
- Optional: `desc`, `category`, `icon`, `env` (a config form shown at install,
  read via `ctx.env`), and `pre_script` / `post_script` (run as root at install).
- Don't bundle `appbase.py`. The device provides the shared SDK.
- Never commit secrets. Declare them in `env` with `"secret": true`.

### Shared code

The Store installs one App directory at a time, so Apps can't import from each other.
Shared modules live in `_shared/` and are **copied** into each App that uses them.
Edit only `_shared/`, then run `python3 _tools/sync_shared.py`. CI fails if a copy is stale.
Start using one with `python3 _tools/sync_shared.py add <app> <module>`.

### Display notes (verified on hardware, 2026-10)

The upstream guide is out of date here. On current firmware both models launch Apps at
`rotate=90` on a **284×240 landscape** canvas, and **all of it is visible** (no hidden
strip). The SDK font is uppercase-only with a few symbols; `_shared/font.py` adds
lowercase and punctuation. At about 200 ppi, scale-1 text is roughly 1 mm tall, so use
scale 2 for anything people need to read. Drawing text is slow, so redraw only when
something changes. The host reserves the left and right 40 px for its exit swipe.

Memory: the Go has about 35 MB free. Keep Apps under about 20 MB RSS and avoid heavy
imports. Apps run as root; the launcher discards stdout/stderr, so use `applog`.

SDK reference: [`base` branch guide](https://github.com/sipeed/NanoKVM-Go-Apps/blob/base/README.md).

## License

[MIT](LICENSE)
