"""Tests for screen-watcher/rules.py (pure detection state machine)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "screen-watcher"))

import rules  # noqa: E402
from rules import Observation, Rules, Settings  # noqa: E402

BLUE, BLACK, DESKTOP = (0, 120, 215), (2, 2, 2), (40, 60, 80)


def frame(color=DESKTOP, cells=48, mixed=False):
    grid = [[color] * 8 for _ in range(cells // 8)]
    if mixed:  # a normal busy screen: lots of variation
        grid = [[((x * 37) % 255, (y * 53) % 255, (x * y) % 255) for x in range(8)] for y in range(cells // 8)]
    return Observation.from_grid(True, 1920, 1080, grid)


NO_SIGNAL = Observation(False)


class ClassifyTest(unittest.TestCase):
    def test_bsod_blue(self):
        self.assertTrue(rules.is_bsod(frame(BLUE)))
        self.assertTrue(rules.is_bsod(frame((0, 0, 170))))  # classic Win9x/XP blue
        self.assertFalse(rules.is_bsod(frame(DESKTOP)))

    def test_bsod_tolerates_some_text(self):
        obs = frame(BLUE)
        grid = [row[:] for row in obs.grid]
        grid[2] = [(255, 255, 255)] * 8  # a row of white text
        self.assertTrue(rules.is_bsod(Observation.from_grid(True, 1920, 1080, grid)))

    def test_black(self):
        self.assertTrue(rules.is_black(frame(BLACK)))
        self.assertFalse(rules.is_black(frame(mixed=True)))

    def test_same_screen_ignores_jpeg_noise(self):
        a = frame(DESKTOP)
        b = Observation.from_grid(True, 1920, 1080, [[(41, 61, 79)] * 8 for _ in range(6)])
        self.assertTrue(rules.same_screen(a, b))
        self.assertFalse(rules.same_screen(a, frame(mixed=True)))
        self.assertFalse(rules.same_screen(a, Observation(True)))


class RulesTest(unittest.TestCase):
    def make(self, **settings):
        return Rules(Settings(**settings))

    def kinds(self, events):
        return [(e.kind, e.level) for e in events]

    def test_signal_loss_needs_prior_signal_and_grace(self):
        r = self.make(signal_grace_s=30)
        self.assertEqual(r.update(NO_SIGNAL, now=0), [])  # never had signal: no alert
        r.update(frame(), now=10)
        self.assertEqual(r.update(NO_SIGNAL, now=20), [])
        self.assertEqual(self.kinds(r.update(NO_SIGNAL, now=51)), [("signal", "alert")])
        self.assertEqual(r.update(NO_SIGNAL, now=90), [])  # once per episode
        events = r.update(frame(), now=120)
        self.assertEqual(self.kinds(events), [("signal", "recovered")])
        self.assertIn("100s", events[0].message)

    def test_short_dropout_is_silent(self):
        r = self.make(signal_grace_s=30)
        r.update(frame(), now=0)
        r.update(NO_SIGNAL, now=5)
        self.assertEqual(r.update(frame(), now=20), [])

    def test_bsod_needs_two_checks(self):
        r = self.make()
        self.assertEqual(r.update(frame(BLUE), now=0), [])
        self.assertEqual(self.kinds(r.update(frame(BLUE), now=30)), [("bsod", "alert")])
        self.assertEqual(self.kinds(r.update(frame(mixed=True), now=60)), [("bsod", "recovered")])

    def test_black_and_frozen_are_opt_in(self):
        r = self.make()
        for t in range(0, 3600, 30):
            self.assertEqual(r.update(frame(BLACK), now=t), [])

    def test_black_after_minutes(self):
        r = self.make(black_min=5)
        r.update(frame(BLACK), now=0)
        self.assertEqual(r.update(frame(BLACK), now=200), [])
        self.assertEqual(self.kinds(r.update(frame(BLACK), now=301)), [("black", "alert")])

    def test_frozen_after_minutes_and_not_when_black(self):
        r = self.make(freeze_min=10, black_min=0)
        r.update(frame(mixed=True), now=0)
        self.assertEqual(r.update(frame(mixed=True), now=500), [])
        self.assertEqual(self.kinds(r.update(frame(mixed=True), now=601)), [("frozen", "alert")])
        r2 = self.make(freeze_min=10)
        r2.update(frame(BLACK), now=0)
        self.assertEqual(r2.update(frame(BLACK), now=700), [])

    def test_signal_gap_does_not_falsely_clear_screen_conditions(self):
        r = self.make()
        r.update(frame(BLUE), now=0)
        r.update(frame(BLUE), now=30)
        self.assertNotIn(("bsod", "recovered"), self.kinds(r.update(NO_SIGNAL, now=60)))
        self.assertIn(("bsod", "recovered"), self.kinds(r.update(frame(mixed=True), now=90)))

    def test_frozen_timer_restarts_after_signal_gap(self):
        r = self.make(freeze_min=10)
        r.update(frame(mixed=True), now=0)
        r.update(NO_SIGNAL, now=500)
        self.assertEqual(r.update(frame(mixed=True), now=700), [])  # same image, but just came back

    def test_bsod_can_be_disabled(self):
        r = self.make(bsod=False)
        for t in (0, 30, 60):
            self.assertEqual(r.update(frame(BLUE), now=t), [])

    def test_state_snapshot_and_restore(self):
        r = self.make()
        r.update(frame(), now=0)
        r.update(frame(BLUE), now=10)
        r.update(frame(BLUE), now=20)
        restored = self.make()
        restored.restore(r.snapshot(), now=100)
        self.assertEqual(restored.active(), ["bsod"])
        # seen_signal survived: signal loss alerts without seeing video again
        restored.update(NO_SIGNAL, now=110)
        self.assertIn(("signal", "alert"), self.kinds(restored.update(NO_SIGNAL, now=150)))

    def test_active_conditions_reported(self):
        r = self.make()
        r.update(frame(BLUE), now=0)
        r.update(frame(BLUE), now=30)
        self.assertEqual(r.active(), ["bsod"])


if __name__ == "__main__":
    unittest.main()
