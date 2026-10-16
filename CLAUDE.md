# NanoKVM Go Apps

App Store catalog for NanoKVM Go / Go+. The device fetches `main` via
`codeload.github.com/.../zip/HEAD` and treats each top-level directory as an App.

- Never add top-level files or directories that aren't Apps unless their name starts with `_` or `.`.
- New App: `python3 _tools/new_app.py <kebab-name> "Display Name"`; app_id prefix `io.github.joshuaaferguson`.
- Before committing: `python3 -m unittest discover -s _tools/tests` and `python3 _tools/validate_catalog.py`.
- SDK docs/types: `./_tools/fetch_sdk.sh` then read `_sdk/README.md` and `_sdk/appbase.pyi`. Never vendor `appbase.py` into an App.
- Apps are stdlib-only Python on the device; extra deps need a `pre_script` (runs as root).
- Geometry (verified): 284x240 landscape at rotate=90, fully visible. The upstream guide's portrait/14px claim is wrong. Keep side 40px free of swipe controls.
- Shared code: edit `_shared/*.py` only, then `python3 _tools/sync_shared.py`. Apps get copies (Store installs one dir at a time).
- Preview without a device: `python3 _tools/preview_app.py <app> out.ppm [--tap X,Y] [--swipe up]`.
- Devices: Go+ 192.168.0.66, Go 192.168.0.72 (SSH key auth as root). On-device logs: `/kvmcomm/apps/<app>/log.txt`.
- Never delete a running App's directory. Stop it first with `pkill -f "[/]kvmcomm/apps/<app>/main.py"`, in a separate ssh call from anything that names the path.
- From this Mac, Homebrew python can't reach LAN IPs (macOS Local Network privacy). Use curl/ssh for device HTTP.
- Hardware behavior (fb, touch, colors) can only be verified on a real device.
