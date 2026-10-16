# NanoKVM Go Apps

App Store catalog for NanoKVM Go / Go+. The device fetches `main` via
`codeload.github.com/.../zip/HEAD` and treats each top-level directory as an App.

- Never add top-level files or directories that aren't Apps unless their name starts with `_` or `.`.
- New App: `python3 _tools/new_app.py <kebab-name> "Display Name"`; app_id prefix `io.github.joshuaaferguson`.
- Before committing: `python3 -m unittest discover -s _tools/tests` and `python3 _tools/validate_catalog.py`.
- SDK docs/types: `./_tools/fetch_sdk.sh` then read `_sdk/README.md` and `_sdk/appbase.pyi`. Never vendor `appbase.py` into an App.
- Apps are stdlib-only Python on the device; extra deps need a `pre_script` (runs as root).
- Layout must respect the 14px hidden edge and keep both side edges free (host exit gesture).
- Hardware behavior (fb, touch, colors) can only be verified on a real device.
