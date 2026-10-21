"""CMTS status accuracy, partial service, identity and shared polling."""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from pysnmp.proto.rfc1902 import OctetString
from pysnmp.proto.rfc1905 import NoSuchInstance, NoSuchObject

import modem_status as status


CMTS = {"id": 1, "name": "CMTS", "address": "192.0.2.1",
        "port": 161, "community": "test"}
MAC = "cc3540e608b1"
MAC_VALUE = OctetString(hexValue=MAC)
LOCATION = (CMTS, "2703362", MAC)


class ModemStatusTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        status._entries.clear()
        status._catalogs.clear()

    def test_cli_statuses_and_init_details(self):
        for cisco, wb, partial, expected in (
            (12, 12, None, "online"), (12, 27, False, "w-online"),
            (12, 27, True, "p-online"), (5, 5, None, "online(d)"),
            (7, 30, True, "p-online(pt)"), (5, 31, False, "w-online(d)"),
            (1, 27, None, "offline"), (3, 27, None, "init(r1)"),
            (4, None, None, "init(d)"), (13, None, None, "init(o)"),
            (18, None, None, "init(i)"), (25, None, None, "shutdown"),
        ):
            with self.subTest(cisco=cisco, wb=wb, partial=partial):
                self.assertEqual(status.decode_state(cisco, 6, wb, partial), expected)
        self.assertIsNone(status.decode_state(12, 6, 27, None))
        self.assertIsNone(status.decode_state(12, 6, NoSuchObject(), None))
        self.assertEqual(status.decode_state(12, 6, NoSuchInstance()), "online")
        self.assertEqual(status.decode_state(5, 6, NoSuchInstance()), "online(d)")
        self.assertIsNone(status.decode_state(NoSuchObject(), NoSuchObject(), NoSuchObject()))

    async def test_full_and_partial_service_read_fresh_channels_by_resil_mac(self):
        for active, impaired, expected in ((8, "", "w-online"),
                                           (7, "137000", "p-online")):
            with self.subTest(active=active):
                # The resiliency index differs from docsIfCmtsCmStatusIndex.
                with (patch.object(status, "_target", AsyncMock(return_value=object())),
                      patch.object(status, "_catalog", AsyncMock(return_value=[{"99": MAC_VALUE}])),
                      patch.object(status, "_get", AsyncMock(side_effect=[
                          [MAC_VALUE, 12, 6, 27],
                          [MAC_VALUE, 8, active, OctetString(impaired)]
                      ])) as get):
                    result = await status._read(LOCATION)
                self.assertEqual(result["status"], expected)
                self.assertEqual(get.await_args_list[1].args[3], [
                    status.RESIL_BASE + "." + str(column) + ".99"
                    for column in (2, 3, 5, 7)])

    async def test_partial_timeout_does_not_become_w_online(self):
        with (patch.object(status, "_target", AsyncMock(return_value=object())),
              patch.object(status, "_get", AsyncMock(return_value=[MAC_VALUE, 12, 6, 27])),
              patch.object(status, "_partial_service", AsyncMock(side_effect=RuntimeError("timeout")))):
            result = await status._read(LOCATION)
        self.assertIsNone(result["status"])
        self.assertEqual(result["error"], "timeout")

    async def test_index_reassignment_never_reads_another_modem_status(self):
        with (patch.object(status, "_target", AsyncMock(return_value=object())),
              patch.object(status, "_get", AsyncMock(return_value=[
                  OctetString(hexValue="001122334455"), 12, 6, 27]))):
            with self.assertRaises(LookupError):
                await status._read(LOCATION)

    async def test_offline_does_not_require_modem_or_partial_service(self):
        with (patch.object(status, "_target", AsyncMock(return_value=object())),
              patch.object(status, "_get", AsyncMock(return_value=[MAC_VALUE, 1, 1, NoSuchInstance()])),
              patch.object(status, "_partial_service", AsyncMock()) as partial):
            result = await status._read(LOCATION)
        self.assertEqual(result["status"], "offline")
        partial.assert_not_awaited()

    async def test_shared_poll_and_next_poll_failure_clear_previous_status(self):
        with (patch.object(status, "_locate_one", AsyncMock(return_value=LOCATION)),
              patch.object(status, "_read", AsyncMock(side_effect=[
                  {"status": "w-online"}, RuntimeError("timeout")])) as read):
            first, second = await asyncio.gather(*(
                status.status_for([CMTS], mac=MAC) for _ in range(2)))
            self.assertEqual(first, second)
            self.assertEqual(read.await_count, 1)
            next(iter(status._entries.values())).updated -= 5
            failed = await status.status_for([CMTS], mac=MAC)
        self.assertIsNone(failed["status"])
        self.assertEqual(failed["error"], "CMTS 192.0.2.1: timeout")
        self.assertEqual(failed["cmts_ip"], "192.0.2.1")
        self.assertEqual(read.await_count, 2)

    async def test_missing_or_ambiguous_modem_is_not_offline(self):
        with patch.object(status, "_locate_one", AsyncMock(return_value=None)):
            result = await status.status_for([CMTS], mac=MAC)
        self.assertIsNone(result["status"])
        status._entries.clear()
        with (patch.object(status, "_locate_one", AsyncMock(return_value=LOCATION)),
              patch.object(status, "_read", AsyncMock()) as read):
            result = await status.status_for([CMTS, {**CMTS, "id": 2}], mac=MAC)
        self.assertIsNone(result["status"])
        read.assert_not_awaited()

    async def test_no_mac_uses_cmts_ip_catalog_and_requires_unique_match(self):
        class Ip:
            def prettyPrint(self):
                return "192.0.2.42"

        with patch.object(status, "_catalog", AsyncMock(return_value=[
            {"42": MAC_VALUE}, {"42": Ip()}
        ])):
            result = await status._locate_one(CMTS, None, {"192.0.2.42"})
        self.assertEqual(result, (CMTS, "42", MAC))

    async def test_missing_pointer_still_finds_offline_mac_without_ip(self):
        with (patch.object(status, "_target", AsyncMock(return_value=object())),
              patch.object(status, "_get", AsyncMock(return_value=[0])),
              patch.object(status, "_catalog", AsyncMock(return_value=[
                  {"42": MAC_VALUE}, {}
              ]))):
            result = await status._locate_one(CMTS, MAC, set())
        self.assertEqual(result, (CMTS, "42", MAC))

    async def test_partial_identity_catalog_refreshes_after_five_seconds(self):
        with (patch.object(status, "_target", AsyncMock(return_value=object())),
              patch.object(status, "_get", AsyncMock(return_value=[NoSuchInstance()])),
              patch.object(status, "_walk", AsyncMock(side_effect=[
                  {"99": MAC_VALUE}, {"100": MAC_VALUE}
              ])) as walk):
            first = await status._catalog(CMTS, partial=True)
            next(iter(status._catalogs.values())).updated -= 5
            second = await status._catalog(CMTS, partial=True)
        self.assertNotEqual(first, second)
        self.assertEqual(walk.await_count, 2)

    async def test_supported_empty_partial_table_has_no_impaired_modems(self):
        with patch.object(status, "_catalog", AsyncMock(return_value=[{}])):
            self.assertFalse(await status._partial_service(CMTS, None, None, MAC))

    async def test_no_such_object_is_cached_and_never_means_full_service(self):
        with (patch.object(status, "_target", AsyncMock(return_value=object())),
              patch.object(status, "_get", AsyncMock(return_value=[NoSuchObject()])) as get,
              patch.object(status, "_walk", AsyncMock()) as walk):
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError, "No Such Object"):
                    await status._partial_service(CMTS, None, None, MAC)
            self.assertEqual(get.await_count, 1)
            walk.assert_not_awaited()
            next(iter(status._catalogs.values())).updated -= 61
            with self.assertRaises(RuntimeError):
                await status._partial_service(CMTS, None, None, MAC)
            self.assertEqual(get.await_count, 2)

    async def test_cli_mode_resolves_snmp_ambiguity_and_refreshes_every_five_seconds(self):
        with (patch.object(status, "cli_configuration", return_value=(True, "maksim")),
              patch.object(status, "_locate_one", AsyncMock(return_value=LOCATION)),
              patch.object(status, "_read", AsyncMock()) as snmp,
              patch.object(status, "cli_status", AsyncMock(side_effect=[
                  "w-online", "p-online", "init(io)", RuntimeError("RSH timeout")
              ])) as cli):
            for expected in ("w-online", "p-online", "init(io)", None):
                for entry in status._entries.values():
                    entry.updated -= 5
                result = await status.status_for([CMTS], mac=MAC)
                self.assertEqual(result["status"], expected)
                if expected is None:
                    self.assertEqual(result["error"], "CMTS 192.0.2.1: RSH timeout")
                    self.assertEqual(result["cmts_ip"], "192.0.2.1")
            self.assertEqual(cli.await_count, 4)
            snmp.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
