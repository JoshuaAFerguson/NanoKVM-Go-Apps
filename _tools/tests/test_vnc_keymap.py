"""Tests for vnc-bridge/keymap.py (X11 keysym -> USB HID boot-keyboard usage)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "vnc-bridge"))

import keymap  # noqa: E402
from keymap import Keyboard  # noqa: E402


class LookupTest(unittest.TestCase):
    def test_letters_digits_and_shifted_symbols(self):
        self.assertEqual(keymap.lookup(ord("a")), (0x04, False))
        self.assertEqual(keymap.lookup(ord("A")), (0x04, True))
        self.assertEqual(keymap.lookup(ord("1")), (0x1E, False))
        self.assertEqual(keymap.lookup(ord("!")), (0x1E, True))
        self.assertEqual(keymap.lookup(ord("?")), (0x38, True))
        self.assertEqual(keymap.lookup(ord(" ")), (0x2C, False))

    def test_special_keys(self):
        self.assertEqual(keymap.lookup(0xFF0D), (0x28, False))  # Return
        self.assertEqual(keymap.lookup(0xFF1B), (0x29, False))  # Escape
        self.assertEqual(keymap.lookup(0xFFBE), (0x3A, False))  # F1
        self.assertEqual(keymap.lookup(0xFFFF), (0x4C, False))  # Delete
        self.assertEqual(keymap.lookup(0xFF52), (0x52, False))  # Up

    def test_modifier_bits(self):
        self.assertEqual(keymap.modifier_bit(0xFFE3), 0x01)  # Control_L
        self.assertEqual(keymap.modifier_bit(0xFFE1), 0x02)  # Shift_L
        self.assertEqual(keymap.modifier_bit(0xFFE9), 0x04)  # Alt_L
        self.assertEqual(keymap.modifier_bit(0xFFEB), 0x08)  # Super_L
        self.assertEqual(keymap.modifier_bit(0xFFEA), 0x40)  # Alt_R
        self.assertIsNone(keymap.modifier_bit(ord("a")))

    def test_unknown(self):
        self.assertIsNone(keymap.lookup(0x10000E9))  # unicode keysym without HID key


class KeyboardTest(unittest.TestCase):
    def test_shared_key_held_until_last_owner_releases(self):
        kb = Keyboard()
        kb.event(0xFFE3, True, owner="a")   # both clients hold Ctrl and "a"
        kb.event(0xFFE3, True, owner="b")
        kb.event(ord("a"), True, owner="a")
        kb.event(ord("a"), True, owner="b")
        report = kb.event(ord("a"), False, owner="a")
        report = kb.event(0xFFE3, False, owner="a")
        self.assertEqual(report[0], 0x01)    # Ctrl still down for b
        self.assertEqual(report[2], 0x04)    # "a" still down for b
        kb.event(0xFFE3, False, owner="b")
        self.assertEqual(kb.event(ord("a"), False, owner="b"), bytes(8))

    def test_simple_press_release(self):
        kb = Keyboard()
        self.assertEqual(kb.event(ord("a"), True), bytes([0, 0, 0x04, 0, 0, 0, 0, 0]))
        self.assertEqual(kb.event(ord("a"), False), bytes(8))

    def test_ctrl_alt_del(self):
        kb = Keyboard()
        kb.event(0xFFE3, True)
        kb.event(0xFFE9, True)
        report = kb.event(0xFFFF, True)
        self.assertEqual(report[0], 0x05)
        self.assertEqual(report[2], 0x4C)

    def test_shifted_symbol_adds_shift_only_while_held(self):
        kb = Keyboard()
        self.assertEqual(kb.event(ord("!"), True)[:3], bytes([0x02, 0, 0x1E]))
        self.assertEqual(kb.event(ord("!"), False), bytes(8))

    def test_client_shift_plus_letter(self):
        kb = Keyboard()
        kb.event(0xFFE1, True)
        self.assertEqual(kb.event(ord("A"), True)[:3], bytes([0x02, 0, 0x04]))
        kb.event(ord("A"), False)
        self.assertEqual(kb.event(0xFFE1, False), bytes(8))

    def test_release_by_other_case_keysym(self):
        # Client presses 'a', then Shift, then releases as 'A': key must still be released.
        kb = Keyboard()
        kb.event(ord("a"), True)
        kb.event(0xFFE1, True)
        self.assertNotIn(0x04, kb.event(ord("A"), False)[2:])

    def test_six_key_rollover_limit(self):
        kb = Keyboard()
        for ch in "abcdefg":
            report = kb.event(ord(ch), True)
        self.assertEqual(len([b for b in report[2:] if b]), 6)

    def test_release_all(self):
        kb = Keyboard()
        kb.event(0xFFE3, True)
        kb.event(ord("c"), True)
        self.assertEqual(kb.release_all(), bytes(8))

    def test_unknown_keysym_changes_nothing(self):
        self.assertIsNone(Keyboard().event(0x10000E9, True))


if __name__ == "__main__":
    unittest.main()
