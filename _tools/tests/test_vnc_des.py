"""Tests for vnc-bridge/des.py against published DES test vectors and VNC auth."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "vnc-bridge"))

import des  # noqa: E402


class DesTest(unittest.TestCase):
    def test_classic_textbook_vector(self):
        # FIPS 46 worked example (Stallings / Grabbe): K=133457799BBCDFF1, P=0123456789ABCDEF
        self.assertEqual(des.encrypt_block(bytes.fromhex("133457799BBCDFF1"), bytes.fromhex("0123456789ABCDEF")).hex(),
                         "85e813540f0ab405")

    def test_nist_sp800_17_vector(self):
        # NIST SP 800-17 variable-plaintext known-answer test, key 0101010101010101
        self.assertEqual(des.encrypt_block(bytes.fromhex("0101010101010101"), bytes.fromhex("8000000000000000")).hex(),
                         "95f8a5e5dd31d900")

    def test_vnc_key_reverses_bits_and_pads(self):
        self.assertEqual(des.vnc_key("ab"), bytes([0x86, 0x46, 0, 0, 0, 0, 0, 0]))  # 'a'=0x61 -> 0x86
        self.assertEqual(len(des.vnc_key("longer-than-eight")), 8)

    def test_vnc_response_is_two_block_ecb(self):
        challenge = bytes(range(16))
        resp = des.vnc_response("secret", challenge)
        key = des.vnc_key("secret")
        self.assertEqual(resp, des.encrypt_block(key, challenge[:8]) + des.encrypt_block(key, challenge[8:]))


if __name__ == "__main__":
    unittest.main()
