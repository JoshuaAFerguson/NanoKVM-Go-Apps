# Text Alert (NanoKVM Go+)

Get a notification when a phrase appears on the target's screen, e.g. **"Build
succeeded"**, **"Kernel panic"**, **"Installation complete"** or **"Update failed"**.

It uses the Go+'s built-in **Memory Fabric**, the on-device NPU OCR that reads the screen
whenever it changes, so nothing is uploaded for recognition. The plain NanoKVM Go has
no NPU or Memory Fabric, so the App says so and does nothing there.

## Setup

1. Web UI → enable **Memory Fabric** and **MCP**, and copy the MCP API key.
2. Install Text Alert and set:
   - **NanoKVM MCP API key**
   - **Phrases to watch for**: separated by `;`, e.g. `Build succeeded; Kernel panic; Installation complete`
   - one or more notification channels (same options as Screen Watcher: ntfy, Discord,
     Pushover, Home Assistant webhook)
3. Open the App and tap **Test** to check notifications.

Matching ignores case, spaces and punctuation, because OCR often drops spaces (it may
read `BUILDSUCCEEDEDin`). Phrases need at least 4 letters or digits. Each phrase alerts
at most once per **Repeat alert after** minutes (default 10), so a message that stays on
screen doesn't spam you.

## What leaves the device

Only the matched phrase and a short snippet of text around it go to your notification
channels. Full OCR text is never logged or sent. When the service starts it skips screen
history, so installing it doesn't replay old screens.

The checking runs in a background service (`nanokvm-text-alert`), so it keeps working while
the App is closed. It removes itself if the App is uninstalled.

Logs: `/kvmcomm/apps/text-alert/service-log.txt`
