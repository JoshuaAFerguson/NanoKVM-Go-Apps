"""Tests for homelab-glance/config.py and httpjson.py."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "homelab-glance"))

import config  # noqa: E402
from config import ConfigError  # noqa: E402


class ConfigTest(unittest.TestCase):
    def test_nothing_configured(self):
        self.assertEqual(config.load({}), [])

    def test_only_configured_services_in_fixed_order(self):
        env = {"DOCKER_URL": "http://10.0.0.5:2375", "HA_URL": "http://ha.lan:8123", "HA_TOKEN": "t",
               "HA_ENTITIES": "sensor.temp, light.desk"}
        services = config.load(env)
        self.assertEqual([s.kind for s in services], ["ha", "docker"])
        self.assertEqual(services[0].options["entities"], ("sensor.temp", "light.desk"))

    def test_url_validation(self):
        for bad in ("ftp://x", "http://user:pw@host", "not a url", "http://"):
            with self.assertRaises(ConfigError, msg=bad):
                config.load({"DOCKER_URL": bad})

    def test_trailing_slash_trimmed(self):
        self.assertEqual(config.load({"DOCKER_URL": "http://d:2375/"})[0].url, "http://d:2375")

    def test_required_extras(self):
        with self.assertRaisesRegex(ConfigError, "HA_TOKEN"):
            config.load({"HA_URL": "http://ha:8123", "HA_ENTITIES": "sensor.x"})
        with self.assertRaisesRegex(ConfigError, "KUMA_SLUG"):
            config.load({"KUMA_URL": "http://kuma:3001"})
        with self.assertRaisesRegex(ConfigError, "PVE_TOKEN"):
            config.load({"PVE_URL": "https://pve:8006"})

    def test_proxmox_token_format(self):
        ok = config.load({"PVE_URL": "https://pve:8006", "PVE_TOKEN": "root@pam!glance=abc-123"})
        self.assertEqual(ok[0].token, "root@pam!glance=abc-123")
        with self.assertRaisesRegex(ConfigError, "PVE_TOKEN"):
            config.load({"PVE_URL": "https://pve:8006", "PVE_TOKEN": "just-a-secret"})

    def test_prometheus_queries(self):
        env = {"PROM_URL": "http://prom:9090", "PROM_QUERIES": "Up=sum(up); Load=node_load1{instance='a'}"}
        q = config.load(env)[0].options["queries"]
        self.assertEqual(q, (("Up", "sum(up)"), ("Load", "node_load1{instance='a'}")))

    def test_prometheus_default_query(self):
        q = config.load({"PROM_URL": "http://prom:9090"})[0].options["queries"]
        self.assertEqual(q[0][0], "Targets up")

    def test_insecure_flag(self):
        s = config.load({"K8S_URL": "https://k8s:6443", "K8S_TOKEN": "t", "K8S_INSECURE": "true"})[0]
        self.assertFalse(s.verify_tls)
        self.assertTrue(config.load({"K8S_URL": "https://k8s:6443", "K8S_TOKEN": "t"})[0].verify_tls)

    def test_tokens_must_be_single_tokens(self):
        for key, base in (("HA_TOKEN", {"HA_URL": "http://ha:8123", "HA_ENTITIES": "sensor.x"}),
                          ("K8S_TOKEN", {"K8S_URL": "https://k:6443"})):
            with self.assertRaisesRegex(ConfigError, key):
                config.load({**base, key: "abc\ndef"})

    def test_url_with_query_or_fragment_rejected(self):
        for bad in ("http://d:2375/?x=1", "http://d:2375/#frag"):
            with self.assertRaises(ConfigError):
                config.load({"DOCKER_URL": bad})

    def test_ca_file_must_exist(self):
        with self.assertRaisesRegex(ConfigError, "PVE_CA_FILE"):
            config.load({"PVE_URL": "https://pve:8006", "PVE_TOKEN": "root@pam!g=s", "PVE_CA_FILE": "/nope.pem"})
        svc = config.load({"PVE_URL": "https://pve:8006", "PVE_TOKEN": "root@pam!g=s", "PVE_CA_FILE": __file__})[0]
        self.assertEqual(svc.ca_file, __file__)

    def test_refresh_bounds(self):
        self.assertEqual(config.refresh_seconds({}), 30)
        self.assertEqual(config.refresh_seconds({"REFRESH_S": "2"}), 10)
        self.assertEqual(config.refresh_seconds({"REFRESH_S": "junk"}), 30)


class HttpJsonTest(unittest.TestCase):
    def test_size_cap(self):
        import httpjson
        with self.assertRaises(httpjson.HttpError):
            httpjson.decode(b"x" * (httpjson.MAX_BYTES + 1), "svc")

    def test_invalid_json(self):
        import httpjson
        with self.assertRaisesRegex(httpjson.HttpError, "JSON"):
            httpjson.decode(b"<html>", "svc")


if __name__ == "__main__":
    unittest.main()
