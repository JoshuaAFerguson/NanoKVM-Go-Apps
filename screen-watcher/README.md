# Screen Watcher

Get a push notification when the machine on your KVM is in trouble, even while the
App is closed:

| Alert | When | Default |
| --- | --- | --- |
| **Video signal lost** | the target stops sending video for `SIGNAL_GRACE_S` (after video was seen) | on, 30 s |
| **Blue screen detected** | most of the screen is BSOD-blue for 2 checks in a row | on |
| **Screen is black** | black with signal for `BLACK_MIN` minutes | off |
| **Screen looks frozen** | nothing changes for `FREEZE_MIN` minutes | off |

Each problem alerts once and sends a "recovered" message when it clears.

Windows 11 24H2 and later shows a **black** stop screen instead of blue. Enable
`BLACK_MIN` to catch it, but note that a black desktop or screensaver will trigger it too.

## Notifications

Set any of these in **Settings → Apps → Installed → Screen Watcher**. Every configured
channel gets each alert.

| Channel | Settings |
| --- | --- |
| [ntfy](https://ntfy.sh) | `NTFY_URL` (e.g. `https://ntfy.sh/<hard-to-guess-topic>` or your own server), optional `NTFY_TOKEN` |
| Discord | `DISCORD_WEBHOOK` (Channel → Integrations → Webhooks) |
| Pushover | `PUSHOVER_TOKEN` (app token) + `PUSHOVER_USER` (user key) |
| Home Assistant | `HA_WEBHOOK_URL` (`http://<ha>:8123/api/webhook/<id>`). The JSON payload has `title`, `message`, `level`, `device` |

Open the App and tap **Test** to send a test alert. **Pause** mutes alerts for an hour
(they still appear in the App).

## How it works

The App installs a background service, `nanokvm-screen-watcher`, which runs at the
lowest CPU priority and is capped at 40 MB. Every `CHECK_S` seconds (default 30) it:

1. reads the HDMI status,
2. grabs a low-quality JPEG snapshot of the full screen from the KVM's video service,
3. averages it into a 16×9 colour grid with a tiny pure-Python decoder that reads only
   each 8×8 block's average colour (no PyAV, which is too heavy for the Go),
4. runs the rules and sends notifications.

On a NanoKVM Go with a 1080p target this costs about **5 % of one CPU core and 12 MB**.
At 4K the checks automatically space out, so the service never uses more than about 10 % of a core.

The touchscreen App only shows status: service state, the last frame's colour, recent
alerts. The watching happens in the service.

Uninstalling the App: the service notices its App is gone and disables itself. To remove
it by hand: `systemctl disable --now nanokvm-screen-watcher && rm /etc/systemd/system/nanokvm-screen-watcher.service`.

Logs: `/kvmcomm/apps/screen-watcher/service-log.txt` (service), `log.txt` (panel).
