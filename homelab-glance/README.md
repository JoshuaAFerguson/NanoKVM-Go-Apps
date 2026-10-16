# Homelab Glance

Six homelab services on one screen: **Home Assistant, Uptime Kuma, Proxmox VE,
Prometheus, Kubernetes, Docker**. Each configured service gets a tile, coloured by health.
Tap a tile for details, tap the header to go back, swipe up/down to page through
details, and tap the footer to refresh now.

A service appears once its URL is set. Everything is configured in the web UI under
**Settings → Apps → Installed → Homelab Glance**.

| Service | Settings | Tile shows |
| --- | --- | --- |
| Home Assistant | `HA_URL`, `HA_TOKEN` (long-lived token: Profile → Security), `HA_ENTITIES` (up to 12 entity IDs) | entities OK / total; detail lists each state |
| Uptime Kuma | `KUMA_URL`, `KUMA_SLUG` (a **public status page** slug) | monitors up / total |
| Proxmox VE | `PVE_URL` (`https://host:8006`), `PVE_TOKEN` as `user@realm!tokenid=secret`, `PVE_INSECURE=true` for the default self-signed cert | nodes online, guests running |
| Prometheus | `PROM_URL`, optional `PROM_QUERIES` (`Label=PromQL; …`, up to 6), optional `PROM_TOKEN` | first query's value |
| Kubernetes | `K8S_URL` (`https://host:6443`), `K8S_TOKEN`, `K8S_INSECURE` | nodes Ready, pod count, problem pods |
| Docker | `DOCKER_URL` (`http://host:2375`) | containers running, unhealthy ones |

`REFRESH_S` sets the poll interval (default 30 s, 10 s minimum).

## Least-privilege tokens

The App only reads, so give it read-only credentials:

- **Proxmox**: Datacenter → Permissions → API Tokens. Add a token for a user with the
  `PVEAuditor` role on `/` (uncheck *Privilege Separation*, or grant the role to the token).
- **Kubernetes**: a ServiceAccount bound to the built-in `view` ClusterRole:
  ```bash
  kubectl create serviceaccount glance -n default
  kubectl create clusterrolebinding glance-view --clusterrole=view --serviceaccount=default:glance
  kubectl create token glance --duration=8760h
  ```
  `view` doesn't cover nodes. For the nodes tile, also bind a ClusterRole with `get, list` on `nodes`.
- **Docker**: don't expose the raw socket. Run
  [docker-socket-proxy](https://github.com/Tecnativa/docker-socket-proxy) with only
  `CONTAINERS=1` and point `DOCKER_URL` at it.

Tokens are stored in the App's configuration on the device and sent only to the URL
you set. Proxies from the environment are ignored and redirects are refused, so a token
can't be forwarded elsewhere.

**TLS:** certificates are verified by default. For a self-signed or private-CA service,
copy its CA certificate to the device (e.g. `scp ca.pem root@<device>:/var/lib/nanokvm-apps/`)
and set `PVE_CA_FILE` / `K8S_CA_FILE` / `HA_CA_FILE` / `DOCKER_CA_FILE`. `*_INSECURE=true` skips
verification entirely. Avoid it where possible: anyone on the LAN could then intercept
the token. `http://` URLs and Docker over plain TCP are unencrypted.

## Limits

- Kubernetes "problem pods" are pods not `Running`/`Succeeded`. A `Running` pod in
  CrashLoopBackOff isn't counted (that would need a much larger query).
- Uptime Kuma: very large status pages (40+ monitors with full heartbeat history) can hit
  the response cap.


Every request has a 5 s timeout and a 512 KB response cap, so a slow or large service
can't stall or exhaust the device. Kubernetes uses small queries: problem pods only,
and a pod count via `limit=1`.

## Testing against your services

`_tools/tests/test_homelab_services.py` runs every fetcher against a local fake server
(`FakeHomelab`). To check against your real services, capture real responses (e.g.
`curl -H "Authorization: Bearer $TOKEN" $HA_URL/api/states/sensor.x`) and paste them in
as new test payloads.

Logs: `ssh root@<device> cat /kvmcomm/apps/homelab-glance/log.txt`
