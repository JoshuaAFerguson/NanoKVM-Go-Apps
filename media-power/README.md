# Media & Power

Two things you'd normally open the web UI for, now on the touchscreen:

- **ISO**: lists the images on the KVM's storage. Tap one (twice, to confirm) to mount it
  on the virtual USB drive as a CD-ROM, read-only, ready to boot an installer. **Eject**
  unmounts it.
- **Power**: with a [Finger Robot](https://wiki.sipeed.com/hardware/en/kvm/NanoKVM_Go/finger_robot.html)
  attached, **Power** gives the target's power button a short press, and **Force off**
  holds it for 6 seconds. Both need a confirming second tap.

Tap the header to switch pages. Swipe up or down in the middle to scroll a long image list.
Upload ISOs through the web UI as usual.

## Setup

Set **KVM web username** and **KVM web password**, using the same login as the KVM's web
page. The App only talks to its own device over the loopback interface, and pins the
device's own HTTPS certificate. The credentials are stored only in the App's
configuration on the device.

If two-factor authentication is enabled on the web UI, the App reports that it can't log in.

## How it works

The App uses the same web API calls as the browser UI. The API isn't documented, and the
calls were taken from the device's web interface, so a firmware update could change it.
If something stops working after an update, check `log.txt`:

`ssh root@<device> cat /kvmcomm/apps/media-power/log.txt`
