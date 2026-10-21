"""Exact CLI state parsing and bounded read-only RSH polling."""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import cmts_cli


MAC = "00252e52d8a6"
CMTS = {"address": "192.0.2.1"}


class CliTests(unittest.IsolatedAsyncioTestCase):
    def test_statuses_from_cli_preserve_exact_state(self):
        for state in ("online", "w-online", "p-online", "online(d)", "init(io)",
                      "init(o)", "offline", "w-online(pt)", "p-online(ptd)"):
            row = f"0025.2e52.d8a6 0.0.0.0 C8/0/4/p {state} 53 !0.50 2071 1 N"
            self.assertEqual(cmts_cli.parse_status(row, MAC), state)
        # /p denotes upstream partial mode and does not determine MAC State.
        self.assertEqual(cmts_cli.parse_status(
            "0025.2e52.d8a6 10.190.0.177 C8/0/4/p w-online 53", MAC), "w-online")

    def test_wrong_missing_and_duplicate_mac_rows_are_not_offline(self):
        row = "0025.2e52.d8a6 0.0.0.0 C8/0/4/U3 offline 53"
        for output in ("Permission denied", "", row.replace("d8a6", "d8a7"), row + "\n" + row):
            with self.assertRaises(RuntimeError):
                cmts_cli.parse_status(output, MAC)

    async def test_read_only_command_uses_exec_arguments_and_validates_mac(self):
        process = self.process(b"0025.2e52.d8a6 10.190.0.177 C8/0/4/UB p-online 53")
        with patch.object(cmts_cli.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)) as spawn:
            self.assertEqual(await cmts_cli.read_status(CMTS, MAC, "maksim"), "p-online")
            self.assertEqual(spawn.await_args.args,
                             ("rsh", "-l", "maksim", "192.0.2.1", "show cable modem 0025.2e52.d8a6"))
            with self.assertRaises(ValueError):
                await cmts_cli.read_status(CMTS, "a;reboot", "maksim")
            self.assertEqual(spawn.await_count, 1)

    @staticmethod
    def process(output, returncode=0):
        class Process:
            def __init__(self):
                self.returncode = None
                self.stdout = asyncio.StreamReader()
                self.stdout.feed_data(output)
                self.stdout.feed_eof()
                self.killed = False

            async def wait(self):
                self.returncode = returncode

            def kill(self):
                self.killed = True
                self.returncode = -9
        return Process()

    async def test_timeout_kills_process_and_has_no_stale_status(self):
        process = self.process(b"")
        process.stdout = asyncio.StreamReader()  # Never produces output or EOF.
        with (patch.object(cmts_cli.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)),
              patch.object(cmts_cli, "TIMEOUT", 0.01)):
            with self.assertRaisesRegex(RuntimeError, "Таймаут"):
                await cmts_cli.read_status(CMTS, MAC)
        self.assertTrue(process.killed)

    async def test_denied_and_oversized_output_do_not_produce_status(self):
        for output, code in ((b"Permission denied", 1), (b"x" * (cmts_cli.MAX_OUTPUT + 1), 0)):
            process = self.process(output, code)
            with patch.object(cmts_cli.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)):
                with self.assertRaises(RuntimeError):
                    await cmts_cli.read_status(CMTS, MAC)


if __name__ == "__main__":
    unittest.main()
