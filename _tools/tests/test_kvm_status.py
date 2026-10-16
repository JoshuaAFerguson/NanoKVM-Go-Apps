"""Tests for kvm-status/probes.py (pure parsers and detectors)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "kvm-status"))

import probes  # noqa: E402
import tiles  # noqa: E402
from tiles import Level, Snapshot  # noqa: E402


class ParseTest(unittest.TestCase):
    def test_hdmi_stable(self):
        hdmi = probes.parse_hdmi({"status": "stable", "width": "2560", "height": "1440", "fps": "60"})
        self.assertEqual(hdmi, probes.Hdmi(True, 2560, 1440, 60))
        self.assertEqual(hdmi.summary, "2560x1440 @60")

    def test_hdmi_no_signal(self):
        hdmi = probes.parse_hdmi({"status": "disappear", "width": "0", "height": "0", "fps": "0"})
        self.assertFalse(hdmi.signal)
        self.assertEqual(hdmi.summary, "No signal")

    def test_hdmi_missing_files(self):
        self.assertFalse(probes.parse_hdmi({}).signal)

    def test_meminfo(self):
        text = "MemTotal:  122168 kB\nMemFree: 3000 kB\nMemAvailable:   38812 kB\nSwapTotal: 60928 kB\nSwapFree: 6000 kB\n"
        mem = probes.parse_meminfo(text)
        self.assertEqual((mem.total_mb, mem.available_mb, mem.swap_used_mb), (119, 37, 53))

    def test_model_from_memory(self):
        self.assertEqual(probes.model_name(119), "NanoKVM Go")
        self.assertEqual(probes.model_name(246), "NanoKVM Go+")

    def test_cpu_percent_from_proc_stat_samples(self):
        a = "cpu  100 0 100 800 0 0 0 0 0 0\n"
        b = "cpu  150 0 150 900 0 0 0 0 0 0\n"
        self.assertEqual(probes.cpu_percent(a, b), 50)

    def test_cpu_percent_no_change(self):
        a = "cpu  100 0 100 800 0 0 0 0 0 0\n"
        self.assertEqual(probes.cpu_percent(a, a), 0)

    def test_iw_link(self):
        text = "Connected to aa:bb (on wlan0)\n\tSSID: Tardis\n\tfreq: 5805.0\n\tsignal: -50 dBm\n"
        wifi = probes.parse_iw_link(text)
        self.assertEqual(wifi, probes.Wifi(True, "Tardis", -50, 5805))
        self.assertEqual(wifi.band, "5G")
        self.assertEqual(wifi.bars, 4)

    def test_iw_not_connected(self):
        self.assertFalse(probes.parse_iw_link("Not connected.\n").connected)

    def test_signal_bars(self):
        self.assertEqual([probes.signal_bars(d) for d in (-45, -60, -70, -80, -90)], [4, 3, 2, 1, 0])

    def test_proc_stat_cpu_ticks(self):
        stat = "489 (kvm_vin) S 1 489 489 0 -1 4194560 0 0 0 0 4100 800 0 0 20 0 9 0 1 0 0"
        self.assertEqual(probes.proc_ticks(stat), 4900)

    def test_proc_stat_with_spaces_in_name(self):
        stat = "12 (my proc) R 1 1 1 0 -1 0 0 0 0 0 10 5 0 0 20 0 1 0 1 0 0"
        self.assertEqual(probes.proc_ticks(stat), 15)

    def test_format_uptime(self):
        self.assertEqual(probes.format_uptime(90800), "1d 1h")
        self.assertEqual(probes.format_uptime(3700), "1h 1m")
        self.assertEqual(probes.format_uptime(59), "0m")


class SpinDetectorTest(unittest.TestCase):
    def test_flags_only_after_sustained_high_cpu_without_signal(self):
        det = probes.SpinDetector(threshold=80, hold_s=30)
        self.assertFalse(det.update(95, signal=False, now=0))
        self.assertFalse(det.update(95, signal=False, now=20))
        self.assertTrue(det.update(95, signal=False, now=31))

    def test_dip_resets(self):
        det = probes.SpinDetector(threshold=80, hold_s=30)
        det.update(95, signal=False, now=0)
        det.update(10, signal=False, now=15)
        self.assertFalse(det.update(95, signal=False, now=40))

    def test_high_cpu_with_signal_is_normal(self):
        det = probes.SpinDetector(threshold=80, hold_s=30)
        det.update(95, signal=True, now=0)
        self.assertFalse(det.update(95, signal=True, now=60))




def snap(**overrides):
    base = dict(model="NanoKVM Go", hdmi=probes.Hdmi(True, 2560, 1440, 60), usb_state="configured",
                usb_speed="high-speed", wifi=probes.Wifi(True, "Tardis", -50, 5805), lan_ip="192.168.0.72",
                tailscale_ip="100.122.94.93", cpu=12, temp_c=49.3, load=0.8,
                mem=probes.Memory(119, 37, 53), uptime_s=90800, disk_free_gb=13.2, vin_stuck=False)
    base.update(overrides)
    return Snapshot(**base)


class TilesTest(unittest.TestCase):
    def by_label(self, snapshot):
        return {t.label: t for t in tiles.build_tiles(snapshot)}

    def test_healthy_snapshot_is_all_ok(self):
        t = self.by_label(snap())
        self.assertEqual(t["Video"].value, "1440p60")
        self.assertEqual(t["USB"].value, "Linked")
        self.assertEqual(t["Wi-Fi"].value, "-50dBm")
        self.assertIn("Tardis", t["Wi-Fi"].detail)
        self.assertTrue(all(tile.level == Level.OK for tile in t.values()))

    def test_no_signal_and_no_usb_warn(self):
        t = self.by_label(snap(hdmi=probes.Hdmi(False), usb_state="not attached"))
        self.assertEqual((t["Video"].value, t["Video"].level), ("None", Level.WARN))
        self.assertEqual((t["USB"].value, t["USB"].level), ("None", Level.WARN))

    def test_hot_cpu_and_low_memory_flagged(self):
        t = self.by_label(snap(temp_c=82.0, mem=probes.Memory(119, 8, 59)))
        self.assertEqual(t["System"].level, Level.ERR)
        self.assertEqual(t["Memory"].level, Level.ERR)

    def test_wifi_down(self):
        t = self.by_label(snap(wifi=probes.Wifi(False), lan_ip=""))
        self.assertEqual(t["Wi-Fi"].level, Level.ERR)

    def test_network_tile_lists_lan_and_tailscale(self):
        t = self.by_label(snap())
        net = t["LAN / Tailnet"]
        self.assertEqual((net.value, net.detail), ("192.168.0.72", "100.122.94.93"))

    def test_detail_lines_fit_a_tile(self):
        # 2x3 grid tile inner width holds 14 glyphs at scale 1
        for tile in tiles.build_tiles(snap(mem=probes.Memory(246, 124, 131))):
            self.assertLessEqual(len(tile.detail), 14, tile)
            self.assertLessEqual(len(tile.label), 14, tile)

    def test_resolution_names(self):
        self.assertEqual(tiles.video_value(probes.Hdmi(True, 3840, 2160, 30)), "4K30")
        self.assertEqual(tiles.video_value(probes.Hdmi(True, 1920, 1080, 60)), "1080p60")
        self.assertEqual(tiles.video_value(probes.Hdmi(True, 3440, 1440, 60)), "3440w60")

    def test_footer_prefers_stuck_video_warning(self):
        self.assertIn("restart", tiles.footer(snap(vin_stuck=True), armed=False).lower())
        self.assertIn("again", tiles.footer(snap(vin_stuck=True), armed=True).lower())
        self.assertIn("up 1d 1h", tiles.footer(snap(), armed=False))



class CollectorRestartTest(unittest.TestCase):
    """restart_video_service must re-check fresh state before sending SIGTERM."""

    def setUp(self):
        import collector
        self.collector_mod = collector
        self.killed = []
        self.patches = {
            "find_pid": lambda name: 489,
            "read_hdmi": lambda: probes.Hdmi(False),
            "read_text": lambda path, default="": "kvm_vin\n" if path.endswith("comm")
            else "489 (kvm_vin) S 1 489 489 0 -1 4194560 100 0 0 0 50 10 0 0 20 0 3 0 777 0 0" if path.endswith("stat") else default,
        }
        self.saved = {k: getattr(probes, k) for k in self.patches}
        for k, v in self.patches.items():
            setattr(probes, k, v)
        self.saved_kill = collector.os.kill
        collector.os.kill = lambda pid, sig: self.killed.append((pid, sig))

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(probes, k, v)
        self.collector_mod.os.kill = self.saved_kill

    def make(self, cpu):
        c = self.collector_mod.Collector(log=lambda m: None)
        c._fresh_vin_cpu = lambda: (489, "777", cpu)
        return c

    def test_does_not_kill_a_replacement_process(self):
        c = self.collector_mod.Collector(log=lambda m: None)
        c._fresh_vin_cpu = lambda: (489, "555", 99)  # measured process had another start time
        msg = c.restart_video_service()
        self.assertEqual(self.killed, [])
        self.assertIn("changed", msg.lower())

    def test_rechecks_video_after_measuring(self):
        c = self.collector_mod.Collector(log=lambda m: None)

        def measure():
            probes.read_hdmi = lambda: probes.Hdmi(True, 1920, 1080, 60)  # signal returns meanwhile
            return 489, "777", 99
        c._fresh_vin_cpu = measure
        msg = c.restart_video_service()
        self.assertEqual(self.killed, [])
        self.assertIn("back", msg.lower())

    def test_kills_when_still_stuck(self):
        msg = self.make(cpu=99).restart_video_service()
        self.assertEqual(len(self.killed), 1)
        self.assertIn("restarting", msg.lower())

    def test_aborts_if_recovered(self):
        msg = self.make(cpu=3).restart_video_service()
        self.assertEqual(self.killed, [])
        self.assertIn("recovered", msg.lower())

    def test_aborts_if_video_returned(self):
        probes.read_hdmi = lambda: probes.Hdmi(True, 1920, 1080, 60)
        self.make(cpu=99).restart_video_service()
        self.assertEqual(self.killed, [])

    def test_aborts_if_pid_now_belongs_to_another_process(self):
        probes.read_text = lambda path, default="": "bash\n" if path.endswith("comm") else default
        self.make(cpu=99).restart_video_service()
        self.assertEqual(self.killed, [])


class ParserRobustnessTest(unittest.TestCase):
    def test_iw_garbage_does_not_raise(self):
        self.assertTrue(probes.parse_iw_link("Connected to x\n\tsignal: \n\tfreq: abc\n").connected)

    def test_cpu_ignores_guest_columns(self):
        a = "cpu  100 0 100 800 0 0 0 0 50 0\n"
        b = "cpu  150 0 150 900 0 0 0 0 100 0\n"
        self.assertEqual(probes.cpu_percent(a, b), 50)

    def test_first_number_handles_blank(self):
        self.assertEqual(probes.first_number(""), 0.0)
        self.assertEqual(probes.first_number("3.74 3.9 4.0 1/2 3"), 3.74)

    def test_ethernet_only_wifi_tile_is_warning_not_error(self):
        t = {x.label: x for x in tiles.build_tiles(snap(wifi=probes.Wifi(False), lan_ip="10.0.0.5"))}
        self.assertEqual(t["Wi-Fi"].level, Level.WARN)


if __name__ == "__main__":
    unittest.main()
