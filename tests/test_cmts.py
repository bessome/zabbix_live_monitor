"""CMTS MAC lookup and parsing regression tests."""
import unittest
from unittest.mock import AsyncMock, patch

import cmts_monitor
import docsis_modem


class _Ip:
    def __init__(self, value):
        self.value = value

    def prettyPrint(self):
        return self.value


class CmtsLookupTests(unittest.IsolatedAsyncioTestCase):
    def test_mac_formats_and_partial_case(self):
        for value in ("CC35.40E8.EE64", "cc:35:40:e8:ee:64",
                      "CC-35-40-E8-EE-64", "cc3540e8ee64"):
            self.assertEqual(cmts_monitor.normalize_mac(value), "cc3540e8ee64")
        self.assertEqual(cmts_monitor.normalize_mac("E8 EE"), "e8ee")
        for bad in ("", "xxyz", "1", "00112233445566"):
            with self.assertRaises(ValueError):
                cmts_monitor.normalize_mac(bad)

    async def test_direct_pointer_then_ip(self):
        cmts = {"id": 3, "name": "Pärnu", "address": "192.0.2.10",
                "port": 161, "community": "public"}
        with patch.object(cmts_monitor, "_target", AsyncMock(return_value=object())), \
             patch.object(cmts_monitor, "_get", AsyncMock(side_effect=[
                 [2966380], [_Ip("10.19.0.223")]
             ])) as get:
            result = await cmts_monitor.resolve_mac(cmts, "CC35.40E8.EE64")
        self.assertEqual(result["ip"], "10.19.0.223")
        self.assertEqual(result["mac"], "cc:35:40:e8:ee:64")
        self.assertEqual(get.await_args_list[0].args[3], [
            cmts_monitor.MAC_POINTER + ".204.53.64.232.238.100"
        ])
        self.assertEqual(get.await_args_list[1].args[3], [
            cmts_monitor.IP_COLUMN + ".2966380"
        ])


    async def test_partial_search_walks_mac_column(self):
        class Oid:
            def prettyPrint(self):
                return cmts_monitor.MAC_COLUMN + ".2966380"

        class Value:
            def asOctets(self):
                return bytes.fromhex("cc3540e8ee64")

        async def walk(*args, **kwargs):
            yield None, None, None, [(Oid(), Value())]

        cmts = {"id": 3, "name": "Pärnu", "address": "192.0.2.10",
                "port": 161, "community": "public"}
        with patch.object(cmts_monitor, "_target",
                          AsyncMock(return_value=object())):
            with patch.object(cmts_monitor, "bulk_walk_cmd", walk):
                with patch.object(cmts_monitor, "_get",
                                  AsyncMock(return_value=[_Ip("10.19.0.223")])):
                    results, truncated = await cmts_monitor.search_cmts(cmts, "E8:EE")
        self.assertFalse(truncated)
        self.assertEqual(results[0]["mac"], "cc:35:40:e8:ee:64")
        self.assertEqual(results[0]["ip"], "10.19.0.223")


    async def test_direct_modem_metrics_are_shared_between_viewers(self):
        docsis_modem._entries.clear()
        snapshot = {"items": [{"id": "us", "label": "US Level",
                               "value": "48.5", "units": "dBmV"}],
                    "updated_at": 1}
        with patch.object(docsis_modem, "_read_channels",
                          AsyncMock(return_value=snapshot)) as read:
            first = await docsis_modem.basic_channels("10.19.0.223", "public")
            second = await docsis_modem.basic_channels("10.19.0.223", "public")
        self.assertEqual(first, second)
        self.assertEqual(read.await_count, 1)


if __name__ == "__main__":
    unittest.main()
