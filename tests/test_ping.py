"""ICMP packet format, parsing, target validation, and rolling statistics."""

import subprocess
import unittest
from unittest.mock import patch

from ping_monitor import PAYLOAD_BYTES, probe, summarize, validate_target


class PingTests(unittest.TestCase):
    def test_windows_probe_sends_128_byte_payload(self):
        output = "Reply from 127.0.0.1: bytes=128 time<1ms TTL=128"
        with patch("ping_monitor.platform.system", return_value="Windows"), \
             patch("ping_monitor.subprocess.run",
                   return_value=subprocess.CompletedProcess([], 0, output, "")) as run:
            self.assertEqual(probe("127.0.0.1"), 0.5)
        self.assertEqual(run.call_args.args[0],
                         ["ping", "-n", "1", "-l", str(PAYLOAD_BYTES),
                          "-w", "900", "127.0.0.1"])

    def test_linux_probe_and_loss(self):
        output = "128 bytes from 127.0.0.1: icmp_seq=1 ttl=64 time=1.25 ms"
        with patch("ping_monitor.platform.system", return_value="Linux"), \
             patch("ping_monitor.subprocess.run",
                   return_value=subprocess.CompletedProcess([], 0, output, "")) as run:
            self.assertEqual(probe("127.0.0.1"), 1.25)
        self.assertEqual(run.call_args.args[0],
                         ["ping", "-c", "1", "-s", "128", "-W", "1", "127.0.0.1"])
        with patch("ping_monitor.platform.system", return_value="Linux"), \
             patch("ping_monitor.subprocess.run",
                   return_value=subprocess.CompletedProcess([], 1, "", "")):
            self.assertIsNone(probe("127.0.0.1"))

    def test_trailing_minute_stats(self):
        samples = [{"at": 39, "ms": 100}, {"at": 41, "ms": 1},
                   {"at": 99, "ms": None}, {"at": 100, "ms": 3}]
        result = summarize(samples, now=100)
        self.assertEqual((result["sent"], result["received"], result["lost"]),
                         (3, 2, 1))
        self.assertEqual((result["min_ms"], result["avg_ms"], result["max_ms"]),
                         (1, 2, 3))
        self.assertEqual(result["loss_pct"], 33.3)
        self.assertEqual(result["current_ms"], 3)

    def test_invalid_target_cannot_be_ping_option(self):
        self.assertTrue(validate_target("192.0.2.5"))
        self.assertTrue(validate_target("host.example.com"))
        self.assertFalse(validate_target("-n"))
        self.assertFalse(validate_target("host name"))
        self.assertFalse(validate_target("—"))


if __name__ == "__main__":
    unittest.main()
