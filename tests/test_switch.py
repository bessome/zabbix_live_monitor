"""Physical switch-port selection and shared SNMP polling."""
import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from switch_monitor import OID, build_ports, poll_ports, snapshot_for


class SwitchTests(unittest.TestCase):
    def test_if_mib_ports_link_states_and_speed_fallback(self):
        columns = {name: {} for name in OID}
        columns["ifOperStatus"] = {3: "2", 1: "1", 2: "1", 4: "1", 5: "1"}
        columns["ifType"] = {1: "6", 2: "6", 3: "6", 4: "135", 5: "6"}
        columns["ifName"] = {1: "Gi1/0/1", 2: "Gi1/0/2", 3: "Gi1/0/3", 4: "Vlan1"}
        columns["ifDescr"] = {5: "Ethernet 5"}
        columns["ifConnectorPresent"] = {1: "1", 2: "1", 3: "1", 4: "2"}
        columns["ifHighSpeed"] = {1: "100", 2: "1000", 3: "1000", 5: "0"}
        columns["ifSpeed"] = {5: "2500000000"}
        ports = build_ports(columns)
        self.assertEqual([item["index"] for item in ports], [1, 2, 3, 5])
        self.assertEqual([item["state"] for item in ports],
                         ["slow", "fast", "down", "fast"])
        self.assertEqual([item["speed_mbps"] for item in ports],
                         [100, 1000, 1000, 2500])
        self.assertEqual(ports[-1]["name"], "Ethernet 5")

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


if __name__ == "__main__":
    unittest.main()
