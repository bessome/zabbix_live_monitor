"""Physical switch-port selection and shared SNMP polling."""
import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from switch_monitor import (DEFAULT_EXCLUDED_NAMES, OID, build_ports,
                             parse_excluded_names, poll_ports, snapshot_for,
                             traffic_for)


class SwitchTests(unittest.TestCase):
    def test_if_mib_ports_link_states_and_speed_fallback(self):
        columns = {name: {} for name in OID}
        columns["ifOperStatus"] = {3: "2", 1: "1", 2: "1", 4: "1", 5: "1"}
        columns["ifType"] = {1: "6", 2: "6", 3: "6", 4: "135", 5: "6"}
        columns["ifName"] = {1: "Gi1/0/1", 2: "Gi1/0/2", 3: "Gi1/0/3", 4: "Vlan1"}
        columns["ifDescr"] = {5: "Ethernet 5"}
        columns["ifConnectorPresent"] = {1: "1", 2: "1", 3: "1", 4: "2", 5: "1"}
        columns["ifHighSpeed"] = {1: "100", 2: "1000", 3: "1000", 5: "0"}
        columns["ifSpeed"] = {5: "2500000000"}
        ports = build_ports(columns)
        self.assertEqual([item["index"] for item in ports], [1, 2, 3, 5])
        self.assertEqual([item["state"] for item in ports],
                         ["slow", "fast", "down", "fast"])
        self.assertEqual([item["speed_mbps"] for item in ports],
                         [100, 1000, 1000, 2500])
        self.assertEqual(ports[-1]["name"], "Ethernet 5")
        self.assertEqual([item["label"] for item in ports], ["1", "2", "3", "5"])

    def test_number_order_excludes_nonphysical_duplicates(self):
        columns = {name: {} for name in OID}
        for index in range(1, 29):
            if_index = 100 + index * 3
            columns["ifOperStatus"][if_index] = "1"
            columns["ifType"][if_index] = "6"
            columns["ifName"][if_index] = f"Gi1/0/{index}"
            columns["ifConnectorPresent"][if_index] = "1"
            columns["ifHighSpeed"][if_index] = "1000"
        for if_index, name in ((1, "1"), (2, "22"), (3, "42")):
            columns["ifOperStatus"][if_index] = "1"
            columns["ifType"][if_index] = "6"
            columns["ifName"][if_index] = name
        ports = build_ports(columns)
        self.assertEqual([port["label"] for port in ports],
                         [str(number) for number in range(1, 29)])

    def test_duplicate_physical_numbers_get_unique_labels(self):
        columns = {name: {} for name in OID}
        columns["ifOperStatus"] = {10: "1", 20: "1"}
        columns["ifType"] = {10: "6", 20: "6"}
        columns["ifName"] = {10: "Gi1/0/1", 20: "Gi2/0/1"}
        columns["ifConnectorPresent"] = {10: "1", 20: "1"}
        ports = build_ports(columns)
        self.assertEqual([port["label"] for port in ports], ["1:1", "2:1"])
        columns["ifName"][20] = "Eth1/0/1"
        ports = build_ports(columns)
        self.assertEqual(len({port["label"] for port in ports}), 2)

    def test_configured_fragments_hide_matching_name_or_description(self):
        columns = {name: {} for name in OID}
        columns["ifOperStatus"] = {index: "1" for index in range(1, 6)}
        columns["ifType"] = {index: "6" for index in range(1, 6)}
        columns["ifName"] = {1: "Gi1/0/1", 2: "Vlan22", 3: "Aux42",
                             4: "Loopback5", 5: "Gi1/0/5"}
        columns["ifDescr"] = {5: "Virtual AUX uplink"}
        excluded = parse_excluded_names(DEFAULT_EXCLUDED_NAMES)
        self.assertEqual(excluded, ("vlan", "aux", "loop"))
        self.assertEqual([port["label"] for port in build_ports(columns, excluded)], ["1"])
        self.assertEqual(len(build_ports(columns)), 5)
        self.assertEqual(parse_excluded_names("(Vlan|AUX|Loop)"), excluded)
        self.assertEqual(parse_excluded_names(""), ())
        with self.assertRaises(ValueError):
            parse_excluded_names("Vlan||Loop")

    def test_direct_if_mib_walk(self):
        rows = {
            "ifDescr": {1: "GigabitEthernet1"},
            "ifType": {1: "6"},
            "ifSpeed": {1: "100000000"},
            "ifOperStatus": {1: "1"},
            "ifName": {1: "Gi1"},
            "ifHighSpeed": {1: "100"},
            "ifConnectorPresent": {1: "1"},
        }

        class Value:
            def __init__(self, value):
                self.value = value

            def prettyPrint(self):
                return str(self.value)

        calls = []

        def fake_walk(*args, **kwargs):
            name = list(OID)[len(calls)]
            calls.append((name, args, kwargs))

            async def results():
                yield None, 0, 0, [
                    (Value(f"{OID[name]}.{index}"), Value(value))
                    for index, value in rows[name].items()
                ]
            return results()

        engine = MagicMock()
        engine.__enter__.return_value = engine
        with (patch("switch_monitor.UdpTransportTarget.create", new_callable=AsyncMock),
              patch("switch_monitor.SnmpEngine", return_value=engine),
              patch("switch_monitor.bulk_walk_cmd", side_effect=fake_walk)):
            ports = asyncio.run(poll_ports("192.0.2.5", 161, "public"))
        self.assertEqual(ports[0]["state"], "slow")
        self.assertEqual(len(calls), len(OID))
        self.assertTrue(all(call[2]["maxRows"] == 2048 for call in calls))

    def test_snapshot_is_shared_for_five_seconds(self):
        with patch("switch_monitor.poll_ports", new_callable=AsyncMock,
                   return_value=[{"index": 1, "name": "Gi1", "state": "fast",
                                  "speed_mbps": 1000}]) as poll:
            async def run():
                first = await snapshot_for("switch-cache", "192.0.2.5", 161, "public")
                second = await snapshot_for("switch-cache", "192.0.2.5", 161, "public")
                return first, second
            first, second = asyncio.run(run())
        self.assertEqual(first, second)
        self.assertEqual(poll.await_count, 1)

    def test_traffic_rate_uses_if_mib_counters_and_shared_five_second_samples(self):
        clock = [100.0]
        samples = [
            {"in_hc": "1000", "out_hc": "2000", "in_32": "1000", "out_32": "2000"},
            {"in_hc": "2250", "out_hc": "5000", "in_32": "2250", "out_32": "5000"},
        ]

        async def poll(address, port, community, definitions):
            self.assertEqual([item["oid"] for item in definitions], [
                "1.3.6.1.2.1.31.1.1.1.6.8", "1.3.6.1.2.1.31.1.1.1.10.8",
                "1.3.6.1.2.1.2.2.1.10.8", "1.3.6.1.2.1.2.2.1.16.8"])
            sample = samples.pop(0)
            return [{"id": key, "value": value} for key, value in sample.items()]

        with (patch("switch_monitor.time.monotonic", side_effect=lambda: clock[0]),
              patch("switch_monitor.poll_values", side_effect=poll) as read):
            async def run():
                first = await traffic_for("traffic-8", 8, "192.0.2.8", 161, "public")
                clock[0] = 104.4
                cached = await traffic_for("traffic-8", 8, "192.0.2.8", 161, "public")
                clock[0] = 105.0
                second = await traffic_for("traffic-8", 8, "192.0.2.8", 161, "public")
                third = await traffic_for("traffic-8", 8, "192.0.2.8", 161, "public")
                return first, cached, second, third
            first, cached, second, third = asyncio.run(run())
        self.assertIsNone(first["down_bps"])
        self.assertIsNone(first["up_bps"])
        self.assertEqual(first, cached)
        self.assertEqual(second, {"down_bps": 4800, "up_bps": 2000})
        self.assertEqual(second, third)
        self.assertEqual(read.call_count, 2)

    def test_traffic_uses_32_bit_fallback_and_clears_sample_after_timeout(self):
        clock = [200.0]
        samples = [
            {"in_hc": None, "out_hc": None, "in_32": "100", "out_32": "200"},
            {"in_hc": None, "out_hc": None, "in_32": "1100", "out_32": "2200"},
            RuntimeError("SNMP timeout"),
            {"in_hc": None, "out_hc": None, "in_32": "3000", "out_32": "4000"},
        ]

        async def poll(*args):
            sample = samples.pop(0)
            if isinstance(sample, Exception):
                raise sample
            return [{"id": key, "value": value} for key, value in sample.items()]

        with (patch("switch_monitor.time.monotonic", side_effect=lambda: clock[0]),
              patch("switch_monitor.poll_values", side_effect=poll)):
            async def run():
                results = []
                for moment in (200, 205, 210, 215):
                    clock[0] = moment
                    results.append(await traffic_for(
                        "traffic-fallback", 9, "192.0.2.9", 161, "public"))
                return results
            first, second, failed, recovered = asyncio.run(run())
        self.assertIsNone(first["down_bps"])
        self.assertEqual(second, {"down_bps": 3200, "up_bps": 1600})
        self.assertEqual(failed["error"], "SNMP timeout")
        self.assertIsNone(failed["down_bps"])
        self.assertIsNone(recovered["down_bps"])


if __name__ == "__main__":
    unittest.main()
