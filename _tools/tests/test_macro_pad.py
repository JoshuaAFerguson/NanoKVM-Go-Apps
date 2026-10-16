"""Tests for macro-pad/macros.py (pure profile/action logic)."""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "macro-pad"))

import macros  # noqa: E402
from macros import Action, MacroError  # noqa: E402


class ParseActionTest(unittest.TestCase):
    def test_key_combo(self):
        action = macros.parse_action({"label": "Lock", "keys": ["MetaLeft", "KeyL"]})
        self.assertEqual(action, Action(label="Lock", keys=("MetaLeft", "KeyL")))

    def test_repeat_action(self):
        action = macros.parse_action({"label": "BIOS", "keys": ["F2"], "repeat": 40, "interval_ms": 250})
        self.assertEqual((action.repeat, action.interval_ms), (40, 250))
        self.assertAlmostEqual(action.duration_s, 10.0)

    def test_text_action(self):
        action = macros.parse_action({"label": "Hi", "text": "hello"})
        self.assertEqual(action.text, "hello")
        self.assertEqual(action.keys, ())

    def test_needs_exactly_one_of_keys_or_text(self):
        for bad in ({"label": "x"}, {"label": "x", "keys": ["KeyA"], "text": "a"}):
            with self.assertRaises(MacroError):
                macros.parse_action(bad)

    def test_rejects_unknown_key_codes(self):
        with self.assertRaisesRegex(MacroError, "Ctrl"):
            macros.parse_action({"label": "x", "keys": ["Ctrl", "KeyC"]})

    def test_text_rejects_repeat_fields(self):
        with self.assertRaises(MacroError):
            macros.parse_action({"label": "x", "text": "a", "repeat": 3})

    def test_hold_ms_parsed(self):
        self.assertEqual(macros.parse_action({"label": "H", "keys": ["AltLeft"], "hold_ms": 800}).hold_ms, 800)

    def test_too_many_keys_rejected(self):
        with self.assertRaises(MacroError):
            macros.parse_action({"label": "x", "keys": ["KeyA"] * 9})

    def test_limits_are_enforced(self):
        with self.assertRaises(MacroError):
            macros.parse_action({"label": "x", "keys": ["F2"], "repeat": 10_000})
        with self.assertRaises(MacroError):
            macros.parse_action({"label": "", "keys": ["F2"]})
        with self.assertRaises(MacroError):
            macros.parse_action({"label": "x", "text": "a" * 2000})


class ProfilesTest(unittest.TestCase):
    def test_builtin_profiles_are_valid_and_named(self):
        profiles = macros.load_profiles(None)
        names = [p.name for p in profiles]
        for expected in ("Windows", "macOS", "Linux", "Mobile", "Nav"):
            self.assertIn(expected, names)
        for profile in profiles:
            self.assertTrue(profile.actions, profile.name)

    def test_custom_list_becomes_custom_profile_first(self):
        custom = json.dumps([{"label": "Hello", "text": "hello"}])
        profiles = macros.load_profiles(custom)
        self.assertEqual(profiles[0].name, "Custom")
        self.assertEqual(profiles[0].actions[0].label, "Hello")

    def test_custom_dict_defines_named_profiles(self):
        custom = json.dumps({"Server": [{"label": "Top", "text": "htop\n"}]})
        self.assertEqual(macros.load_profiles(custom)[0].name, "Server")

    def test_duplicate_or_empty_profile_names_rejected(self):
        for custom in ({"A": [{"label": "x", "text": "x"}], "a": [{"label": "y", "text": "y"}]},
                       {"  ": [{"label": "x", "text": "x"}]},
                       {"Windows": [{"label": "x", "text": "x"}]}):
            with self.assertRaises(MacroError):
                macros.load_profiles(json.dumps(custom))

    def test_macos_recovery_uses_cmd_r(self):
        mac = next(p for p in macros.load_profiles(None) if p.name == "macOS")
        recover = next(a for a in mac.actions if a.label == "Recover")
        self.assertEqual(recover.keys, ("MetaLeft", "KeyR"))

    def test_invalid_custom_json_raises_with_context(self):
        with self.assertRaisesRegex(MacroError, "CUSTOM_MACROS"):
            macros.load_profiles("[not json")

    def test_order_profiles_puts_default_first(self):
        profiles = macros.load_profiles(None)
        ordered = macros.start_index(profiles, "linux")
        self.assertEqual(profiles[ordered].name, "Linux")
        self.assertEqual(macros.start_index(profiles, "nope"), 0)


class DescribeTest(unittest.TestCase):
    def test_combo_label_uses_profile_meta_name(self):
        action = Action(label="Lock", keys=("ControlLeft", "MetaLeft", "KeyQ"))
        self.assertEqual(macros.describe(action, meta="Cmd"), "Ctrl+Cmd+Q")

    def test_repeat_and_text_descriptions(self):
        self.assertEqual(macros.describe(Action(label="b", keys=("F2",), repeat=40, interval_ms=250)), "F2 10s")
        self.assertEqual(macros.describe(Action(label="b", keys=("F2",), repeat=3, interval_ms=100)), "F2 x3")
        self.assertEqual(macros.describe(Action(label="h", keys=("AltLeft",), repeat=15, hold_ms=1000)), "hold Alt")
        self.assertEqual(macros.describe(Action(label="t", text="hello world")), '"hello wo~')


class PagingTest(unittest.TestCase):
    def test_pages_split_actions(self):
        actions = tuple(Action(label=str(i), keys=("KeyA",)) for i in range(7))
        pages = macros.paginate(actions, per_page=6)
        self.assertEqual([len(p) for p in pages], [6, 1])


class ErrorMessageTest(unittest.TestCase):
    def test_hid_timeout_is_explained(self):
        msg = macros.friendly_error("write /dev/hidg0: write /dev/hidg0: i/o timeout")
        self.assertIn("target", msg.lower())

    def test_401_matched_as_http_status_only(self):
        self.assertIn("key", macros.friendly_error("MCP tools/call failed (HTTP 401)").lower())
        self.assertEqual(macros.friendly_error("value 4010 out of range"), "value 4010 out of range")

    def test_other_errors_pass_through_truncated(self):
        self.assertEqual(macros.friendly_error("unknown key: Foo"), "unknown key: Foo")


if __name__ == "__main__":
    unittest.main()


class BuiltinLabelFitTest(unittest.TestCase):
    """Every built-in label must render at scale 2 in a real 2x3 Macro Pad cell."""

    def test_builtin_labels_fit_at_scale_two(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_shared"))
        import ui

        header, rest = ui.Box(0, 0, 284, 240).split_top(28)
        body, _ = rest.split_bottom(18)
        cell = ui.grid(body.inset(4), 2, 3, 4)[0]
        label_box = ui.button_label_box(cell, True)
        too_big = [a.label for p in macros.load_profiles(None) for a in p.actions
                   if ui.label_layout(a.label, label_box.w, label_box.h)[0] < 2]
        self.assertEqual(too_big, [])
