import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import modem_reboot as reboot


class ModemRebootTests(unittest.IsolatedAsyncioTestCase):
    async def test_cmts_reset_uses_audit_and_shares_modem_cooldown(self):
        cmts = {"address": "192.0.2.1", "name": "CMTS"}
        begin, finish = MagicMock(return_value=19), MagicMock()
        with (patch.dict("sys.modules", {"app_storage": SimpleNamespace(
                  begin_modem_reboot=begin, finish_modem_reboot=finish)}),
              patch("cmts_cli.configuration", return_value=(True, "maksim")),
              patch("modem_status.locate_for_reset", AsyncMock(return_value=(cmts, "001122334455", "192.0.2.42"))),
              patch("cmts_cli.reset_modem", AsyncMock(return_value={"outcome": "accepted"})) as reset):
            await reboot.reboot_via_cmts({"username": "Operator"}, [cmts], device={"name": "Modem"})
            reset.assert_awaited_once_with(cmts, "192.0.2.42", "maksim")
            begin.assert_called_once_with({"username": "Operator"}, "Modem / 192.0.2.42",
                                          "192.0.2.1", 514, "RSH / CMTS CMTS")
            finish.assert_called_once_with(19, "accepted")
            with self.assertRaises(reboot.RebootBusy):
                await reboot.reboot_modem("192.0.2.42", 161, "private")
            self.send.assert_not_awaited()

    def setUp(self):
        reboot._recent.clear()
        self.transport = patch.object(reboot.UdpTransportTarget, "create", AsyncMock())
        self.create = self.transport.start()
        self.addCleanup(self.transport.stop)
        self.command = patch.object(reboot, "set_cmd", AsyncMock(return_value=(None, 0, 0, [])))
        self.send = self.command.start()
        self.addCleanup(self.command.stop)
        self.engine = patch.object(reboot, "SnmpEngine", MagicMock())
        self.engine.start()
        self.addCleanup(self.engine.stop)

    async def test_standard_reset_is_sent_once_and_duplicate_is_blocked(self):
        result = await reboot.reboot_modem("192.0.2.42", 161, "private")
        self.assertEqual(result["outcome"], "accepted")
        self.create.assert_awaited_once_with(("192.0.2.42", 161), timeout=3.0, retries=0)
        args = self.send.await_args.args
        self.assertEqual(args[1].communityName, "private")
        # Resolve numeric OID locally; no device or MIB download is involved.
        from pysnmp.smi.builder import MibBuilder
        from pysnmp.smi.view import MibViewController
        varbind = args[4].resolve_with_mib(MibViewController(MibBuilder()))
        self.assertEqual(str(varbind[0]), reboot.RESET_OID)
        self.assertEqual(int(varbind[1]), 1)
        with self.assertRaises(reboot.RebootBusy):
            await reboot.reboot_modem("192.0.2.42", 161, "other")
        self.send.assert_awaited_once()

    async def test_no_response_does_not_claim_success_or_retry(self):
        self.send.return_value = ("timeout", 0, 0, [])
        self.assertEqual((await reboot.reboot_modem("192.0.2.42", 161, "private"))["outcome"], "unknown")
        self.send.assert_awaited_once()

    async def test_deadline_is_unknown(self):
        self.send.side_effect = asyncio.TimeoutError()
        self.assertEqual((await reboot.reboot_modem("192.0.2.42", 161, "private"))["outcome"], "unknown")

    async def test_agent_rejection(self):
        status = MagicMock()
        status.prettyPrint.return_value = "notWritable"
        self.send.return_value = (None, status, 1, [])
        with self.assertRaisesRegex(RuntimeError, "notWritable"):
            await reboot.reboot_modem("192.0.2.42", 161, "private")

    async def test_invalid_target_never_sends(self):
        with self.assertRaises(ValueError):
            await reboot.reboot_modem("bad;address", 161, "private")
        self.send.assert_not_awaited()

    async def test_audit_is_committed_before_command_and_retains_unknown_result(self):
        begin, finish = MagicMock(return_value=17), MagicMock()
        with patch.dict("sys.modules", {"app_storage": SimpleNamespace(
                begin_modem_reboot=begin, finish_modem_reboot=finish)}):
            async def send(address, port, community):
                begin.assert_called_once_with({"username": "Operator"}, "Modem", "192.0.2.42", 161, "CMTS 1")
                finish.assert_not_called()
                return {"outcome": "unknown"}
            await reboot.logged_reboot({"username": "Operator"}, "Modem", "192.0.2.42", 161,
                                       "secret", "CMTS 1", send)
            finish.assert_called_once_with(17, "unknown")

    async def test_audit_records_failure_without_secret_or_exception_text(self):
        for error, outcome in [(reboot.RebootBusy(), "blocked"), (RuntimeError("secret"), "rejected"),
                               (OSError("secret"), "unknown")]:
            finish = MagicMock()
            with patch.dict("sys.modules", {"app_storage": SimpleNamespace(
                    begin_modem_reboot=MagicMock(return_value=18), finish_modem_reboot=finish)}):
                with self.assertRaises(type(error)):
                    await reboot.logged_reboot({"username": "Operator"}, "Modem", "192.0.2.42", 161,
                                               "secret", "CMTS 1", AsyncMock(side_effect=error))
                finish.assert_called_once_with(18, outcome)
