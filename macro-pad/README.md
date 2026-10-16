# Macro Pad

Touch buttons on the NanoKVM screen that send key combos, BIOS/boot keys and
text to the machine plugged into the KVM.

| Gesture | Action |
| --- | --- |
| Tap a button | Send it. Green flash = sent, red = failed (reason in the footer) |
| Tap the header | Next profile (Windows → macOS → Linux → Mobile → Nav → …) |
| Swipe up / down (middle of screen) | Next / previous page |
| Swipe left / right (middle of screen) | Next / previous profile |
| Tap a running boot-key button | Cancel it |

Stripe colours: amber = key combo, violet = boot keys, blue = typed text.
Boot-key buttons tap the key every 250 ms for 15 s (BIOS, boot menu, GRUB Esc), or hold it
(Mac Boot Picker / Recovery, GRUB Shift). Press one, then power on or reboot the target.
Holds are sent as back-to-back 1-second `holdMs` presses. Whether the device honours `holdMs`
hasn't been verified on a booting target yet; if it doesn't, they act as rapid taps. Keep swipes away from the screen edges, which the
host reserves for its exit gesture.

## Setup

1. Web UI → **Settings → MCP**: turn MCP on and copy the API key.
2. Install Macro Pad from the Store and paste the key into **NanoKVM MCP API key**.

The key only ever goes to the device's own MCP server (`https://127.0.0.1/api/mcp`).

## Custom macros

Set **Custom macros (JSON)** to a list of macros (shown as a "Custom" profile) or to an
object of named lists (one profile each). Custom profiles appear before the built-ins.

```json
{
  "Proxmox": [
    {"label": "Shell", "keys": ["ControlLeft", "AltLeft", "KeyT"]},
    {"label": "Status", "text": "pveversion -v\n"},
    {"label": "BIOS F11", "keys": ["F11"], "repeat": 60, "interval_ms": 250}
  ]
}
```

| Field | Meaning |
| --- | --- |
| `label` | Button text, 1 to 14 chars. Words of 7 or fewer chars keep the large font |
| `keys` | One combo, as [KeyboardEvent.code](https://developer.mozilla.org/docs/Web/API/UI_Events/Keyboard_event_code_values) names: `ControlLeft`, `ShiftLeft`, `AltLeft`, `MetaLeft`, `KeyA`, `Digit1`, `F2`, `Enter`, `Escape`, `ArrowUp`, … |
| `text` | Text to type instead of `keys` (up to 500 chars; `\n` = Enter) |
| `repeat` | Send the combo this many times (1 to 200) |
| `interval_ms` | Gap between repeats (50 to 5000, default 250) |
| `hold_ms` | Hold the combo this long per press (1 to 5000). Combine with `repeat` and `interval_ms` to hold through a boot |

Don't put passwords in `text`: the value is stored in the App's configuration on the device.

## Troubleshooting

- **"No target: keyboard not accepted"**: the KVM's USB keyboard isn't being read.
  The target is unplugged, asleep, or doesn't enumerate USB HID on that port.
- **"MCP key rejected"**: re-copy the key from Settings → MCP into the App settings.
- Logs: `ssh root@<device> cat /kvmcomm/apps/macro-pad/log.txt`
