"""TP-Link cable-test OIDs, port selection and result parsing."""
import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from cable_test import (FLUSH_OID, RESULT_BASE, CableTestBusy, decode_pairs,
                        port_identifier, run_cable_test)


class Value:
    def __init__(self, value):
        self.value = value

    def prettyPrint(self):
        return self.value


class CableTests(unittest.TestCase):
    def test_port_id_uses_documented_tp_link_index(self):
        self.assertEqual(port_identifier("Gi1/0/8"), ("1/0/8", 49160))
        self.assertEqual(port_identifier("GigabitEthernet 1/0/5"), ("1/0/5", 49157))
        for invalid in ("Vlan8", "Gi2/0/8", "Gi1/1/8", "Gi1/0/0", "Gi1/0/2048"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                port_identifier(invalid)

    def test_pairs_show_length_including_fault_distance(self):
        index = 49160
        bindings = [
            (Value(f"{RESULT_BASE}.{column}.{index}"), Value(value))
            for column, value in ((2, "Normal"), (3, "66 m"),
                                  (4, "Open"), (5, "22 (+/- 2m)"))
        ]
        self.assertEqual(decode_pairs(bindings, index), [
            {"pair": "Pair-A", "status": "Normal", "length": "66 m", "error": "—"},
            {"pair": "Pair-B", "status": "Open", "length": "22 (+/- 2m)",
             "error": "—"},
        ])

    def test_test_sets_port_then_reads_its_result_after_five_seconds(self):
        index = 49160
        bindings = [(Value(f"{RESULT_BASE}.{column}.{index}"), Value(value))
                    for column, value in ((2, "Normal"), (3, "66 m"),
                                          (4, "Normal"), (5, "66 m"))]
        engine = MagicMock()
        engine.__enter__.return_value = engine
        with (patch("cable_test.UdpTransportTarget.create", new_callable=AsyncMock),
              patch("cable_test.SnmpEngine", return_value=engine),
              patch("cable_test.ObjectIdentity", side_effect=lambda oid: oid),
              patch("cable_test.ObjectType", side_effect=lambda oid, value=None: (oid, value)),
              patch("cable_test.get_cmd", new_callable=AsyncMock,
                    side_effect=[(None, 0, 0, [(None, Value("1.3.6.1.4.1.11863.6"))]),
                                 (None, 0, 0, bindings)]) as get,
              patch("cable_test.set_cmd", new_callable=AsyncMock,
                    return_value=(None, 0, 0, [])) as set_command,
              patch("cable_test.asyncio.sleep", new_callable=AsyncMock) as sleep):
            result = asyncio.run(run_cable_test("host-cable", "192.0.2.5", 161,
                                                "read-test", "write-test", "Gi1/0/8"))
        self.assertEqual(result["port"], "1/0/8")
        self.assertEqual(len(result["pairs"]), 2)
        self.assertEqual(sleep.await_args.args, (5,))
        self.assertEqual(set_command.await_count, 1)
        set_args = set_command.await_args.args
        self.assertEqual(set_args[1].communityName, "write-test")
        self.assertEqual(set_args[4][0], FLUSH_OID)
        self.assertEqual(set_args[4][1].prettyPrint(), "1/0/8")
        self.assertEqual(get.await_count, 2)
        self.assertEqual(get.await_args.args[4][0], f"{RESULT_BASE}.2.{index}")

    def test_rejects_non_tp_link_before_set(self):
        engine = MagicMock()
        engine.__enter__.return_value = engine
        with (patch("cable_test.UdpTransportTarget.create", new_callable=AsyncMock),
              patch("cable_test.SnmpEngine", return_value=engine),
              patch("cable_test.get_cmd", new_callable=AsyncMock,
                    return_value=(None, 0, 0, [(None, Value("1.3.6.1.4.1.9.1"))])),
              patch("cable_test.set_cmd", new_callable=AsyncMock) as set_command):
            with self.assertRaises(ValueError):
                asyncio.run(run_cable_test("host-not-tplink", "192.0.2.5", 161,
                                            "public", "private", "Gi1/0/8"))
        set_command.assert_not_awaited()

    def test_only_one_test_at_a_time_per_switch(self):
        import cable_test
        cable_test._running.add("busy-host")
        try:
            with self.assertRaises(CableTestBusy):
                asyncio.run(run_cable_test("busy-host", "192.0.2.5", 161,
                                            "public", "private", "Gi1/0/8"))
        finally:
            cable_test._running.discard("busy-host")


if __name__ == "__main__":
    unittest.main()
