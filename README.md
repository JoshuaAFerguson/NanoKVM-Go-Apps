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
| [`hello-nanokvm`](hello-nanokvm/) | Smoke test: confirms this Store installs and runs Apps. |

## Repository layout

```text
.
├── hello-nanokvm/          # one directory per App (the catalog)
│   ├── app.json
│   └── main.py
├── _template/              # starter App copied by _tools/new_app.py
├── _tools/
│   ├── new_app.py          # scaffold a new App with a matching app_id
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
python3 _tools/validate_catalog.py
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

### Display notes

The panel is 284×240 physical with 14 hidden pixels. Apps normally run at
`rotate=90` on a 240×284 logical canvas, where the bottom 14 rows are hidden.
Lay out against the visible rect (see `_template/main.py`) and keep the left
and right edges free of swipe controls, because the host reserves them for the
exit gesture.

SDK reference: [`base` branch guide](https://github.com/sipeed/NanoKVM-Go-Apps/blob/base/README.md).

## License

[MIT](LICENSE)
