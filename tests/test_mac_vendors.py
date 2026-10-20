"""Offline attribution and safe, persistent MAC database updates."""
import gzip
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import mac_vendors as vendors


class MacVendorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.bundle = self.root / "bundled.json.gz"
        self.live = self.root / "instance" / "mac-vendors.json.gz"
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.addCleanup(self.temp.cleanup)
        self.stack.enter_context(patch.object(vendors, "BUNDLED_PATH", self.bundle))
        self.stack.enter_context(patch.object(vendors, "database_path", return_value=self.live))
        self.stack.enter_context(patch.object(vendors, "MIN_PREFIXES", 1))
        self.stack.enter_context(patch.object(vendors, "_cached", None))
        self.source = {
            "001122000000/24": [{"t": "add", "d": "2001-01-01", "o": "Original"},
                                {"t": "change", "d": "2020-01-01", "o": "Current vendor"}],
            "001122300000/28": [{"t": "add", "d": "2010-01-01", "o": "Medium vendor"}],
            "001122334000/36": [{"t": "add", "d": "2015-01-01", "o": "Small vendor"}],
            "001133000000/24": [{"t": "add", "d": "2001-01-01", "o": "Deleted vendor"},
                                {"t": "delete", "d": "2020-01-01", "o": "Deleted vendor"}],
        }
        self.data = vendors.snapshot_from_source(json.dumps(self.source).encode())
        self.write(self.bundle, self.data)

    def write(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(gzip.compress(json.dumps(data).encode()))

    def response(self, raw):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = raw
        return response

    def test_longest_prefix_current_owner_and_earliest_date_work_offline(self):
        with patch.object(vendors.urlrequest, "urlopen") as network:
            result = vendors.lookup_macs(["00:11:22:33:44:55", "0011.2235.4455",
                                          "00-11-22-44-55-66", "00:11:33:00:00:01"])
        self.assertEqual([row["vendor"] for row in result],
                         ["Small vendor", "Medium vendor", "Current vendor", None])
        self.assertEqual(result[2]["first_seen"], "2001-01-01")
        self.assertEqual(result[0]["first_seen"], "2015-01-01")
        network.assert_not_called()

    def test_local_multicast_invalid_and_missing_addresses_are_not_attributed(self):
        result = vendors.lookup_macs(["02:11:22:33:44:55", "01:11:22:33:44:55", "bad"])
        self.assertEqual([row["kind"] for row in result], ["local", "multicast", "unknown"])
        self.assertTrue(all(row["vendor"] is None and row["first_seen"] is None for row in result))
        self.bundle.unlink()
        self.assertEqual(vendors.lookup_macs(["00:11:22:33:44:55"])[0]["kind"], "unavailable")

    def test_download_is_atomic_persistent_and_invalid_data_preserves_previous_copy(self):
        changed = json.loads(json.dumps(self.source))
        changed["001122334000/36"].append({"t": "change", "d": "2025-01-01", "o": "New vendor"})
        raw = json.dumps(changed).encode()
        with patch.object(vendors.urlrequest, "urlopen", return_value=self.response(raw)) as fetch:
            count = vendors.update_database()
        self.assertEqual(count, 3)
        self.assertEqual(fetch.call_args.args[0].full_url, vendors.SOURCE_URL)
        self.assertEqual(vendors.lookup_macs(["00:11:22:33:44:55"])[0]["vendor"], "New vendor")
        self.assertFalse(vendors.database_status()["bundled"])
        saved = self.live.read_bytes()
        for raw in (b"<html>Bad Gateway</html>", b"{}", b'{"bad": []}'):
            with (self.subTest(raw=raw),
                  patch.object(vendors.urlrequest, "urlopen", return_value=self.response(raw))):
                with self.assertRaises(RuntimeError):
                    vendors.update_database()
                self.assertEqual(self.live.read_bytes(), saved)
        with patch.object(vendors.urlrequest, "urlopen", side_effect=TimeoutError):
            with self.assertRaises(RuntimeError):
                vendors.update_database()
        self.assertEqual(self.live.read_bytes(), saved)
        self.assertEqual(list(self.live.parent.glob("*.tmp")), [])

    def test_failed_replace_keeps_working_copy_and_releases_update_lock(self):
        self.write(self.live, self.data)
        saved = self.live.read_bytes()
        with (patch.object(vendors.urlrequest, "urlopen",
                           return_value=self.response(json.dumps(self.source).encode())),
              patch.object(vendors.os, "replace", side_effect=OSError("read only"))):
            with self.assertRaises(RuntimeError):
                vendors.update_database()
        self.assertEqual(self.live.read_bytes(), saved)
        self.assertEqual(list(self.live.parent.glob("*.tmp")), [])
        self.assertFalse(vendors._update_lock.locked())

    def test_corrupt_override_falls_back_to_bundled_copy(self):
        self.live.parent.mkdir()
        self.live.write_bytes(b"corrupt")
        self.assertEqual(vendors.database_status()["count"], 3)
        self.assertTrue(vendors.database_status()["bundled"])
        self.assertTrue(vendors.database_status()["error"])
        self.assertEqual(vendors.lookup_macs(["00:11:22:33:44:55"])[0]["vendor"], "Small vendor")

    def test_source_validation_rejects_bad_dates_masks_and_short_downloads(self):
        for source in (
            {"001122334455/24": self.source["001122000000/24"]},
            {"001122000000/24": [{"d": "2020-02-30", "t": "add", "o": "Vendor"}]},
        ):
            with self.subTest(source=source), self.assertRaises(ValueError):
                vendors.snapshot_from_source(json.dumps(source).encode())
        with patch.object(vendors, "MIN_PREFIXES", 100):
            with self.assertRaises(ValueError):
                vendors.snapshot_from_source(json.dumps(self.source).encode())

    def test_bundled_snapshot_contains_real_prefixes_and_valid_metadata(self):
        bundled = Path(__file__).resolve().parents[1] / "data" / "mac-vendors.json.gz"
        data = vendors._read_snapshot(bundled)
        self.assertGreater(len(data["prefixes"]), 10000)
        self.assertIn("cisco", data["prefixes"]["00000c"][0].lower())


if __name__ == "__main__":
    unittest.main()
