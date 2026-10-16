"""Tests for wol-panel/wol.py (pure Wake-on-LAN and ping helpers)."""

import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "wol-panel"))

import wol  # noqa: E402
from monitor import Monitor  # noqa: E402
from wol import Host, WolError  # noqa: E402


class ParseHostsTest(unittest.TestCase):
    def test_compact_syntax(self):
        hosts = wol.parse_hosts("NAS=AA:BB:CC:DD:EE:FF@192.168.0.10; Desktop=11-22-33-44-55-66")
        self.assertEqual(hosts, (
            Host("NAS", "aa:bb:cc:dd:ee:ff", "192.168.0.10"),
            Host("Desktop", "11:22:33:44:55:66", ""),
        ))

    def test_newlines_and_blank_entries(self):
        self.assertEqual(len(wol.parse_hosts("A=aa:bb:cc:dd:ee:01\n\nB=aa:bb:cc:dd:ee:02;")), 2)

    def test_json_syntax(self):
        hosts = wol.parse_hosts('[{"name": "NAS", "mac": "aabbccddeeff", "ip": "10.0.0.2"}]')
        self.assertEqual(hosts[0], Host("NAS", "aa:bb:cc:dd:ee:ff", "10.0.0.2"))

    def test_empty_is_no_hosts(self):
        self.assertEqual(wol.parse_hosts(""), ())
        self.assertEqual(wol.parse_hosts(None), ())

    def test_errors_name_the_entry(self):
        for bad, needle in (("NAS=zz:bb:cc:dd:ee:ff", "NAS"), ("NAS", "NAS"),
                            ("NAS=aa:bb:cc:dd:ee:ff@999.1.1.1", "999"), ("=aa:bb:cc:dd:ee:ff", "name")):
            with self.assertRaisesRegex(WolError, needle):
                wol.parse_hosts(bad)

    def test_special_ips_and_bad_types_rejected(self):
        for bad in ("A=aa:bb:cc:dd:ee:ff@0.0.0.0", "A=aa:bb:cc:dd:ee:ff@127.0.0.1",
                    "A=aa:bb:cc:dd:ee:ff@224.0.0.1", "A=aa:bb:cc:dd:ee:ff@255.255.255.255",
                    "A=aa:bb cc-dd.ee:ff", '[{"name": "A", "mac": 112233445566}]'):
            with self.assertRaises(WolError, msg=bad):
                wol.parse_hosts(bad)

    def test_duplicate_names_rejected(self):
        with self.assertRaises(WolError):
            wol.parse_hosts("A=aa:bb:cc:dd:ee:01;a=aa:bb:cc:dd:ee:02")


class PacketTest(unittest.TestCase):
    def test_magic_packet_layout(self):
        pkt = wol.magic_packet("aa:bb:cc:dd:ee:ff")
        self.assertEqual(len(pkt), 102)
        self.assertEqual(pkt[:6], b"\xff" * 6)
        self.assertEqual(pkt[6:12], bytes.fromhex("aabbccddeeff"))
        self.assertEqual(pkt[6:], bytes.fromhex("aabbccddeeff") * 16)

    def test_directed_broadcast(self):
        self.assertEqual(wol.directed_broadcast("192.168.0.72", "255.255.255.0"), "192.168.0.255")
        self.assertEqual(wol.directed_broadcast("10.1.2.3", "255.255.0.0"), "10.1.255.255")

    def test_icmp_checksum_validates(self):
        packet = wol.icmp_echo(ident=0x1234, seq=7, payload=b"nanokvm")
        self.assertEqual(wol.inet_checksum(packet), 0)  # a correct checksum sums to zero
        self.assertEqual(packet[0], 8)  # echo request

    def test_parse_echo_reply_matches_id_and_seq(self):
        reply = bytearray(wol.icmp_echo(ident=0x1234, seq=7, payload=b"x"))
        reply[0] = 0  # echo reply
        ip_header = bytes([0x45]) + bytes(19)
        self.assertTrue(wol.is_echo_reply(ip_header + bytes(reply), ident=0x1234, seq=7))
        self.assertFalse(wol.is_echo_reply(ip_header + bytes(reply), ident=0x1234, seq=8))


class BroadcastValidationTest(unittest.TestCase):
    def test_accepts_global_and_private_ipv4(self):
        good, bad = wol.validate_broadcasts(["255.255.255.255", "10.0.1.255", " 192.168.5.255 "])
        self.assertEqual((good, bad), (["255.255.255.255", "10.0.1.255", "192.168.5.255"], []))

    def test_rejects_hostnames_public_and_garbage(self):
        good, bad = wol.validate_broadcasts(["example.com", "8.8.8.8", "192.168.1.300", "::1", ""])
        self.assertEqual(good, [])
        self.assertEqual(len(bad), 4)  # blank entries are just skipped


class IcmpSafetyTest(unittest.TestCase):
    def test_short_or_bogus_ip_header_rejected(self):
        self.assertFalse(wol.is_echo_reply(b"\x00" * 30, 0, 0))
        self.assertFalse(wol.is_echo_reply(b"\x45" + b"\x00" * 10, 0, 0))

    def test_payload_nonce_must_match(self):
        reply = bytearray(wol.icmp_echo(ident=1, seq=1, payload=b"nonce-A"))
        reply[0] = 0
        packet = bytes([0x45]) + bytes(19) + bytes(reply)
        self.assertTrue(wol.is_echo_reply(packet, 1, 1, payload=b"nonce-A"))
        self.assertFalse(wol.is_echo_reply(packet, 1, 1, payload=b"nonce-B"))


class ArpTest(unittest.TestCase):
    def test_parse_arp_table_skips_incomplete(self):
        text = ("IP address       HW type     Flags       HW address            Mask     Device\n"
                "192.168.0.1      0x1         0x2         10:20:30:40:50:60     *        wlan0\n"
                "192.168.0.9      0x1         0x0         00:00:00:00:00:00     *        wlan0\n"
                "100.100.100.100  0x1         0x2         aa:aa:aa:aa:aa:aa     *        tailscale0\n")
        self.assertEqual(wol.parse_arp(text), [("192.168.0.1", "10:20:30:40:50:60")])


class StatusTextTest(unittest.TestCase):
    def test_states(self):
        self.assertEqual(wol.status_text(up=True, waking_since=None, now=0), "online")
        self.assertEqual(wol.status_text(up=False, waking_since=None, now=0), "offline")
        self.assertEqual(wol.status_text(up=None, waking_since=None, now=0, has_ip=False), "no IP to ping")
        self.assertEqual(wol.status_text(up=None, waking_since=None, now=0, has_ip=True), "checking...")
        self.assertEqual(wol.status_text(up=False, waking_since=10, now=33), "waking 23s")



class FakeClock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class MonitorTest(unittest.TestCase):
    def setUp(self):
        self.hosts = (Host("NAS", "aa:bb:cc:dd:ee:ff", "10.0.0.2"), Host("Box", "aa:bb:cc:dd:ee:01", ""))
        self.up = {"10.0.0.2": False}
        self.sent = []
        self.clock = FakeClock()
        self.mon = Monitor(self.hosts, ping=lambda ips, timeout: {ip: self.up[ip] for ip in ips},
                           send=lambda mac: self.sent.append(mac) or 6, clock=self.clock, log=lambda m: None)

    def test_host_without_ip_is_unknown(self):
        self.mon.poll_once()
        self.assertIsNone(self.mon.state("Box").up)

    def test_wake_sends_and_marks_waking(self):
        self.mon.wake(self.hosts[0])
        self.assertEqual(self.sent, ["aa:bb:cc:dd:ee:ff"])
        self.assertIsNotNone(self.mon.state("NAS").waking_since)

    def test_coming_up_reports_elapsed_and_clears_waking(self):
        self.mon.wake(self.hosts[0])
        self.clock.t += 41
        self.up["10.0.0.2"] = True
        self.mon.poll_once()
        state = self.mon.state("NAS")
        self.assertTrue(state.up)
        self.assertIsNone(state.waking_since)
        self.assertIn("41s", self.mon.message)

    def test_waking_gives_up_after_timeout(self):
        self.mon.wake(self.hosts[0])
        self.clock.t += 500
        self.mon.poll_once()
        self.assertIsNone(self.mon.state("NAS").waking_since)
        self.assertIn("no response", self.mon.message)

    def test_poll_interval_is_faster_while_waking(self):
        self.assertGreater(self.mon.interval(), 2)
        self.mon.wake(self.hosts[0])
        self.assertLessEqual(self.mon.interval(), 2)

    def test_wake_without_ip_does_not_enter_waking(self):
        self.mon.wake(self.hosts[1])
        self.assertIsNone(self.mon.state("Box").waking_since)
        self.assertIn("no IP", self.mon.message)

    def test_poll_failure_is_surfaced(self):
        def broken(ips, timeout):
            raise PermissionError("raw socket")
        mon = Monitor(self.hosts, ping=broken, send=lambda mac: 1, clock=self.clock, log=lambda m: None)
        mon.run_once()
        self.assertIn("raw socket", mon.message)

    def test_messages_expire(self):
        self.mon.wake(self.hosts[0])
        self.assertTrue(self.mon.current_message())
        self.clock.t += 60
        self.assertEqual(self.mon.current_message(), "")

    def test_version_bumps_on_change(self):
        v = self.mon.version
        self.mon.wake(self.hosts[0])
        self.assertGreater(self.mon.version, v)


if __name__ == "__main__":
    unittest.main()
