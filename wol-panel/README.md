# Wake-on-LAN

Tap a machine to wake it. Machines with an IP are pinged, so each tile shows
**online**, **offline**, or **waking 23s** while it boots. The footer then reports how
long the wake took.

## Setup

In the web UI open **Settings → Apps → Installed → Wake-on-LAN** and fill in **Machines**:

```
NAS=aa:bb:cc:dd:ee:ff@192.168.0.10; Desktop=11:22:33:44:55:66@192.168.0.20; Laptop=66:55:44:33:22:11
```

- `Name=MAC[@IP]`, separated by `;` or new lines. Names are 1 to 14 characters.
- The IP is optional and only used for the online status.
- A JSON list also works: `[{"name": "NAS", "mac": "aa:bb:cc:dd:ee:ff", "ip": "192.168.0.10"}]`.
- With no machines configured, the App lists the LAN neighbours it can see (IP + MAC),
  which makes them easy to copy.

Packets go to `255.255.255.255` and to the local subnet broadcast (e.g. `192.168.0.255`)
on UDP ports 9 and 7. Use **Extra broadcast addresses** for other subnets your router
forwards to.

## Making a machine wakeable

The target needs Wake-on-LAN enabled in its BIOS/UEFI and in the OS network driver
(Windows: adapter → Power Management; Linux: `ethtool -s eth0 wol g`). Most machines
only wake over **wired Ethernet**.

Swipe up/down in the middle of the screen for more pages (6 machines per page).
Logs: `ssh root@<device> cat /kvmcomm/apps/wol-panel/log.txt`
