"""Modem channel selection, SNMP scaling, and polling behavior."""
import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from pysnmp.proto.rfc1902 import Integer

from snmp_monitor import (channel_definition, display_modem_values,
                          link_error_rates, optical_definition, poll_values,
                          snapshot_for)


class SnmpTests(unittest.TestCase):
    def test_optical_input_power_uses_zabbix_oid_and_multiplier(self):
        item = {"itemid": "81", "name": "Optical input power", "type": "20",
                "status": "0", "snmp_oid": "get[1.3.6.1.4.1.11195.1.5.5.1.4.1]",
                "units": "dBm", "value_type": "0",
                "preprocessing": [{"type": "1", "params": "0.1"}]}
        definition = optical_definition(item)
        self.assertEqual((definition["id"], definition["label"], definition["oid"],
                          definition["multiplier"], definition["units"]),
                         ("81", "Optical input power", "1.3.6.1.4.1.11195.1.5.5.1.4.1",
                          "0.1", "dBm"))
        self.assertIsNone(optical_definition({**item, "name": "Optical output power"}))
        self.assertIsNone(optical_definition({**item, "status": "1"}))

    def test_channel_names_oid_and_zabbix_multiplier(self):
        base = {"itemid": "55", "type": "20", "status": "0", "units": "dB",
                "preprocessing": [{"type": "1", "params": "0.1"}]}
        upstream = channel_definition({
            **base, "name": "Upstream channel US2 Level",
            "snmp_oid": "get[1.3.6.1.4.1.100.2]",
        })
        downstream = channel_definition({
            **base, "name": "Downstream channel 52 459MHz SNR",
            "snmp_oid": "get[1.3.6.1.4.1.100.52]",
        })
        self.assertEqual((upstream["label"], upstream["oid"], upstream["multiplier"]),
                         ("US2 Level", "1.3.6.1.4.1.100.2", "0.1"))
        self.assertEqual(downstream["label"], "DS52 459MHz SNR")
        self.assertEqual(channel_definition({**base,
            "name": "Downstream channel 3 447MHz Level",
            "snmp_oid": "get[1.3.6.1.4.1.100.3]"})["label"],
            "DS3 447MHz Level")
        generic = [
            ("Upstream channel US Level", "US Level"),
            ("Downstream channel Level", "DS Level"),
            ("Downstream channel SNR", "DS SNR"),
        ]
        for name, label in generic:
            with self.subTest(name=name):
                definition = channel_definition({**base, "name": name,
                                                 "snmp_oid": ".1.3.6.1.4.1.100.3"})
                self.assertEqual((definition["label"], definition["oid"],
                                  definition["multiplier"], definition["units"]),
                                 (label, "1.3.6.1.4.1.100.3", "0.1", "dB"))
        self.assertIsNone(channel_definition({**base, "name": "Downstream channel 52 Traffic",
                                              "snmp_oid": "get[1.3.6.1.4.1.100.52]"}))
        self.assertIsNone(channel_definition({**base, "name": "Downstream channel 52 459MHz SNR",
                                              "snmp_oid": "get[not-an-oid]"}))
        self.assertIsNone(channel_definition({**base, "name": "Downstream channel 52 459MHz SNR",
                                               "snmp_oid": "get[1.2.3]",
                                               "preprocessing": [{"type": "5", "params": "x"}]}))

    def test_error_rate_pairs_with_matching_snr_channel(self):
        base = {"type": "20", "status": "0", "snmp_oid": "get[1.2.3]",
                "units": "", "value_type": "0", "preprocessing": []}
        names = [
            ("1", "Downstream channel 52 459MHz SNR"),
            ("2", "Downstream channel 52 459MHz ErrorRate"),
            ("3", "Downstream channel 53 471MHz SNR"),
            ("4", "Downstream channel 53 ErrorRate"),
            ("5", "Downstream channel 54 ErrorRate"),
        ]
        definitions = [channel_definition({**base, "itemid": item_id, "name": name})
                       for item_id, name in names]
        link_error_rates(definitions)
        self.assertEqual(definitions[0]["error_rate_id"], "2")
        self.assertEqual(definitions[2]["error_rate_id"], "4")
        values = [{"id": item_id, "label": definition["label"], "value": value,
                   "units": definition["units"]}
                  for (item_id, _), definition, value in zip(
                      names, definitions, ("33.9", "2", "34.1", "3", "7"))]
        displayed = display_modem_values(definitions, values)
        self.assertEqual([item["id"] for item in displayed], ["1", "3"])
        self.assertEqual([item["error_rate"] for item in displayed], ["2", "3"])

    def test_error_rate_change_per_second_uses_two_snmp_samples(self):
        base = {"itemid": "90", "name": "Downstream channel 52 ErrorRate",
                "type": "20", "status": "0", "snmp_oid": "get[1.2.3]",
                "units": "", "value_type": "0",
                "preprocessing": [{"type": "10", "params": ""}]}
        definition = channel_definition(base)
        self.assertTrue(definition["rate"])
        with (patch("snmp_monitor.poll_values", new_callable=AsyncMock,
                    side_effect=[[{"id": "90", "label": definition["label"],
                                  "value": "100", "units": ""}],
                                 [{"id": "90", "label": definition["label"],
                                   "value": "118", "units": ""}]]) as poll,
              patch("snmp_monitor.time") as clock):
            clock.monotonic.side_effect = [0, 0, 1, 2, 8, 9, 10, 11]
            clock.time.return_value = 100
            async def sample():
                first = await snapshot_for("rate-host", "192.0.2.5", 161,
                                           "public", [definition])
                second = await snapshot_for("rate-host", "192.0.2.5", 161,
                                            "public", [definition])
                return first, second
            first, second = asyncio.run(sample())
        self.assertIsNone(first["items"][0]["value"])
        self.assertEqual(second["items"][0]["value"], "2")
        self.assertEqual(poll.await_count, 2)

    def test_direct_snmp_get_applies_multiplier(self):
        definition = {"id": "55", "label": "US2 Level", "oid": "1.3.6.1.4.1.100.2",
                      "multiplier": "0.1", "units": "mV"}
        engine = MagicMock()
        engine.__enter__.return_value = engine
        with (patch("snmp_monitor.UdpTransportTarget.create", new_callable=AsyncMock) as target,
              patch("snmp_monitor.SnmpEngine", return_value=engine),
              patch("snmp_monitor.get_cmd", new_callable=AsyncMock) as get):
            get.return_value = (None, 0, 0, [(None, Integer(327))])
            result = asyncio.run(poll_values("192.0.2.5", 161, "public", [definition]))
        self.assertEqual(result[0]["value"], "32.7")
        self.assertEqual(target.call_args.kwargs, {"timeout": 3.0, "retries": 0})
        self.assertEqual(get.await_count, 1)

    def test_shared_snapshot_uses_five_second_cache(self):
        definition = {"id": "55", "label": "US2 Level", "oid": "1.2.3",
                      "multiplier": "1", "units": "mV"}
        with patch("snmp_monitor.poll_values", new_callable=AsyncMock,
                   return_value=[{"id": "55", "label": "US2 Level",
                                  "value": "32.7", "units": "mV"}]) as poll:
            async def run():
                first = await snapshot_for("test-host-cache", "192.0.2.5", 161,
                                           "public", [definition])
                second = await snapshot_for("test-host-cache", "192.0.2.5", 161,
                                            "public", [definition])
                return first, second
            first, second = asyncio.run(run())
        self.assertEqual(first, second)
        self.assertEqual(poll.await_count, 1)


if __name__ == "__main__":
    unittest.main()
