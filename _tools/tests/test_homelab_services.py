"""Tests for homelab-glance/services.py: parsers on documented payloads, plus an
end-to-end run against a local fake server that mimics all six APIs.

To test against your real services, copy FakeHomelab's payloads from real
responses (curl the same paths) - see homelab-glance/README.md.
"""

import json
import socketserver
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "homelab-glance"))

import config  # noqa: E402
import services  # noqa: E402
from services import Level  # noqa: E402

HA_STATES = {
    "sensor.living_temp": {"entity_id": "sensor.living_temp", "state": "21.5",
                           "attributes": {"friendly_name": "Living Room", "unit_of_measurement": "°C"}},
    "light.desk": {"entity_id": "light.desk", "state": "on", "attributes": {"friendly_name": "Desk Lamp"}},
    "sensor.broken": {"entity_id": "sensor.broken", "state": "unavailable", "attributes": {}},
}
KUMA_PAGE = {"publicGroupList": [{"name": "Services", "monitorList": [
    {"id": 1, "name": "Website"}, {"id": 2, "name": "NAS"}, {"id": 3, "name": "VPN"}]}]}
KUMA_BEATS = {"heartbeatList": {"1": [{"status": 0}, {"status": 1}], "2": [{"status": 1}, {"status": 0}],
                                "3": [{"status": 3}]}}
PVE_RESOURCES = {"data": [
    {"type": "node", "node": "pve1", "status": "online", "cpu": 0.12, "maxmem": 64e9, "mem": 32e9},
    {"type": "node", "node": "pve2", "status": "offline"},
    {"type": "qemu", "vmid": 100, "name": "web", "status": "running", "node": "pve1"},
    {"type": "lxc", "vmid": 101, "name": "dns", "status": "stopped", "node": "pve1"},
    {"type": "qemu", "vmid": 9000, "name": "tmpl", "status": "stopped", "node": "pve1", "template": 1},
    {"type": "storage", "storage": "local", "status": "available", "disk": 50e9, "maxdisk": 100e9},
]}
PROM = {"sum(up)": {"status": "success", "data": {"resultType": "vector", "result": [{"metric": {}, "value": [1, "9"]}]}},
        "count(up)": {"status": "success", "data": {"resultType": "vector", "result": [{"metric": {}, "value": [1, "10"]}]}}}
K8S_NODES = {"items": [
    {"metadata": {"name": "n1"}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}},
    {"metadata": {"name": "n2"}, "status": {"conditions": [{"type": "Ready", "status": "False"}]}}]}
K8S_ALL = {"metadata": {"remainingItemCount": 40}, "items": [{"metadata": {"name": "p"}}]}
K8S_FILTERED_NO_COUNT = {"metadata": {}, "items": [{"metadata": {"name": "p"}}]}  # selectors drop the count
K8S_PROBLEMS = {"items": [{"metadata": {"name": "api-7f9", "namespace": "prod"}, "status": {"phase": "Pending"}}]}
DOCKER = [{"Names": ["/web"], "State": "running", "Status": "Up 2 hours (healthy)"},
          {"Names": ["/db"], "State": "running", "Status": "Up 2 hours (unhealthy)"},
          {"Names": ["/old"], "State": "exited", "Status": "Exited (1) 3 days ago"}]


class FakeHomelab(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 (http.server API)
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        auth = self.headers.get("Authorization", "")
        path = url.path
        if path.startswith("/api/states/"):
            if auth != "Bearer ha-token":
                return self._send({"message": "unauthorized"}, 401)
            entity = path.rsplit("/", 1)[1]
            return self._send(HA_STATES[entity]) if entity in HA_STATES else self._send({}, 404)
        if path == "/api/status-page/home":
            return self._send(KUMA_PAGE)
        if path == "/api/status-page/heartbeat/home":
            return self._send(KUMA_BEATS)
        if path == "/api2/json/cluster/resources":
            ok = auth == "PVEAPIToken=root@pam!glance=s3cret"
            return self._send(PVE_RESOURCES) if ok else self._send({}, 401)
        if path == "/api/v1/query":
            return self._send(PROM[query["query"][0]])
        if path == "/api/v1/nodes":
            return self._send(K8S_NODES)
        if path == "/api/v1/pods":
            if "fieldSelector" not in query:
                return self._send(K8S_ALL)
            return self._send(K8S_PROBLEMS)
        if path.startswith("/redirect"):
            self.send_response(302)
            self.send_header("Location", "http://example.invalid/steal")
            self.end_headers()
            return None
        if path == "/containers/json":
            return self._send(DOCKER)
        return self._send({}, 404)


class LocalServer(ThreadingHTTPServer):
    """Skips HTTPServer's reverse-DNS lookup (getfqdn), which can stall for ~35 s."""

    daemon_threads = True

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "localhost", self.server_address[1]


def by_label(result):
    return {row.label: row for row in result.rows}


class ParserTest(unittest.TestCase):
    def test_home_assistant(self):
        result = services.parse_ha(list(HA_STATES.values()))
        rows = by_label(result)
        self.assertEqual(rows["Living Room"].value, "21.5°C")
        self.assertEqual(rows["sensor.broken"].level, Level.WARN)
        self.assertEqual(result.level, Level.WARN)
        self.assertEqual(result.summary, "2/3")

    def test_uptime_kuma_uses_latest_beat(self):
        result = services.parse_kuma(KUMA_PAGE, KUMA_BEATS)
        rows = by_label(result)
        self.assertEqual((rows["Website"].value, rows["NAS"].value, rows["VPN"].value), ("up", "DOWN", "maint"))
        self.assertEqual((result.summary, result.level), ("1/3 up", Level.ERR))

    def test_proxmox(self):
        result = services.parse_pve(PVE_RESOURCES)
        self.assertEqual(result.summary, "1/2 nodes")
        self.assertEqual(result.level, Level.ERR)
        self.assertIn("1/2 guests", result.detail)
        self.assertEqual(by_label(result)["101 dns"].value, "stopped")

    def test_prometheus(self):
        result = services.parse_prom([("Targets up", PROM["sum(up)"]), ("Targets", PROM["count(up)"])])
        self.assertEqual(by_label(result)["Targets up"].value, "9")
        self.assertEqual(result.summary, "9")

    def test_prometheus_number_formatting(self):
        self.assertEqual(services.fmt_number("0.123456"), "0.123")
        self.assertEqual(services.fmt_number("1234567"), "1.23M")
        self.assertEqual(services.fmt_number("NaN"), "NaN")

    def test_kubernetes(self):
        result = services.parse_k8s(K8S_NODES, K8S_ALL, K8S_PROBLEMS)
        self.assertEqual(result.summary, "1/2 nodes")
        self.assertIn("41 pods", result.detail)
        self.assertEqual(by_label(result)["prod/api-7f9"].value, "Pending")
        self.assertEqual(result.level, Level.ERR)

    def test_docker(self):
        result = services.parse_docker(DOCKER)
        self.assertEqual(result.summary, "2/3 up")
        self.assertEqual(by_label(result)["db"].level, Level.ERR)
        self.assertEqual(by_label(result)["web"].value, "healthy")

    def test_proxmox_skips_templates_and_flags_no_nodes(self):
        self.assertNotIn("9000 tmpl", by_label(services.parse_pve(PVE_RESOURCES)))
        empty = services.parse_pve({"data": []})
        self.assertEqual(empty.level, Level.WARN)
        self.assertIn("ACL", empty.detail)

    def test_null_fields_become_strings(self):
        result = services.parse_pve({"data": [{"type": "node", "node": None, "status": None}]})
        self.assertTrue(all(isinstance(r.label, str) and isinstance(r.value, str) for r in result.rows))

    def test_number_format_avoids_exponents(self):
        self.assertEqual(services.fmt_number("1234.5"), "1.23k")
        self.assertEqual(services.fmt_number("9999.9"), "10k")
        self.assertEqual(services.fmt_number("1e999"), "1e999")

    def test_prometheus_string_result(self):
        resp = {"status": "success", "data": {"resultType": "string", "result": [1, "hello"]}}
        self.assertEqual(services.parse_prom([("S", resp)]).rows[0].value, "hello")

    def test_garbage_payloads_raise_parse_error(self):
        for fn, args in ((services.parse_pve, ({"data": "x"},)), (services.parse_docker, ({"no": "list"},)),
                         (services.parse_kuma, ([], {})), (services.parse_k8s, ({}, [], {}))):
            with self.assertRaises(services.ParseError):
                fn(*args)


class EndToEndTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = LocalServer(("127.0.0.1", 0), FakeHomelab)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def env(self, **extra):
        env = {"HA_URL": self.base, "HA_TOKEN": "ha-token", "HA_ENTITIES": "sensor.living_temp,light.desk",
               "KUMA_URL": self.base, "KUMA_SLUG": "home",
               "PVE_URL": self.base, "PVE_TOKEN": "root@pam!glance=s3cret",
               "PROM_URL": self.base, "K8S_URL": self.base, "K8S_TOKEN": "k", "DOCKER_URL": self.base}
        env.update(extra)
        return env

    def test_all_six_services_fetch(self):
        results = [services.fetch(s) for s in config.load(self.env())]
        self.assertEqual([r.title for r in results],
                         ["Home Asst", "Uptime", "Proxmox", "Prometheus", "Kubernetes", "Docker"])
        self.assertTrue(all(not r.error for r in results), [r.error for r in results])

    def test_bad_token_becomes_error_result_not_exception(self):
        svc = config.load(self.env(HA_TOKEN="wrong"))[0]
        result = services.fetch(svc)
        self.assertEqual(result.level, Level.ERR)
        self.assertIn("401", result.error)

    def test_k8s_count_uses_unfiltered_list(self):
        svc = config.load({"K8S_URL": self.base, "K8S_TOKEN": "k"})[0]
        self.assertIn("41 pods", services.fetch(svc).detail)

    def test_missing_ha_entity_named_in_error(self):
        svc = config.load(self.env(HA_ENTITIES="sensor.living_temp,sensor.gone"))[0]
        result = services.fetch(svc)
        self.assertIn("sensor.gone", result.error)

    def test_redirects_are_refused(self):
        svc = config.load({"DOCKER_URL": self.base + "/redirect"})[0]
        result = services.fetch(svc)
        self.assertIn("redirect", result.error.lower())

    def test_error_never_contains_token(self):
        svc = config.load(self.env(HA_TOKEN="sekret-token-value"))[0]
        svc = services.Service(**{**svc.__dict__, "url": "http://127.0.0.1:1"})
        self.assertNotIn("sekret", services.fetch(svc).error)

    def test_unreachable_service_is_error_result(self):
        svc = config.load({"DOCKER_URL": "http://127.0.0.1:1"})[0]
        self.assertEqual(services.fetch(svc).level, Level.ERR)


if __name__ == "__main__":
    unittest.main()
