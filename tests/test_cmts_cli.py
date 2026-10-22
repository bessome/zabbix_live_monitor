"""Exact CLI state parsing and bounded read-only RSH polling."""
import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

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

    def test_hash_marker_in_live_cmts_output_is_preserved(self):
        output = ("D MAC Address IP Address I/F MAC Prim RxPwr Timing Num I State\n"
                  "2cab.a41c.6944 10.19.1.123 C5/1/1/UB #w-online 56 0.00 1331 1 N\n")
        self.assertEqual(cmts_cli.parse_status(output, "2caba41c6944"), "#w-online")
        for state in ("#p-online", "#online(d)", "#offline", "#init(o)"):
            self.assertEqual(cmts_cli.parse_status(output.replace("#w-online", state),
                                                  "2caba41c6944"), state)

    async def test_single_channel_request_has_real_local_identity_and_no_callback(self):
        reader, writer = self.connection(b"\0" + b"0025.2e52.d8a6 10.190.0.177 C8/0/4/UB p-online 53")
        with (patch.object(cmts_cli, "_connect", AsyncMock(return_value=(reader, writer))) as connect,
              patch.object(cmts_cli, "_local_user", return_value="monitor")):
            self.assertEqual(await cmts_cli.read_status(CMTS, MAC, "maksim"), "p-online")
            writer.write.assert_called_once_with(
                b"0\0monitor\0maksim\0show cable modem 0025.2e52.d8a6\0")
            with self.assertRaises(ValueError):
                await cmts_cli.read_status(CMTS, "a;reboot", "maksim")
            self.assertEqual(connect.await_count, 1)
        writer.close.assert_called_once()
        writer.wait_closed.assert_awaited_once()

    @staticmethod
    def connection(output=None):
        reader = asyncio.StreamReader()
        if output is not None:
            reader.feed_data(output)
            reader.feed_eof()
        writer = Mock()
        writer.drain = AsyncMock()
        writer.wait_closed = AsyncMock()
        return reader, writer

    async def test_timeout_closes_connection_and_has_no_stale_status(self):
        reader, writer = self.connection()  # Never produces output or EOF.
        with (patch.object(cmts_cli, "_connect", AsyncMock(return_value=(reader, writer))),
              patch.object(cmts_cli, "_local_user", return_value="monitor"),
              patch.object(cmts_cli, "TIMEOUT", 0.01)):
            with self.assertRaisesRegex(RuntimeError, "Таймаут"):
                await cmts_cli.read_status(CMTS, MAC)
        writer.close.assert_called_once()

    async def test_denied_and_oversized_output_do_not_produce_status(self):
        for output in (b"\1Access denied.\n", b"\0" + b"x" * (cmts_cli.MAX_OUTPUT + 1)):
            reader, writer = self.connection(output)
            with (patch.object(cmts_cli, "_connect", AsyncMock(return_value=(reader, writer))),
                  patch.object(cmts_cli, "_local_user", return_value="monitor")):
                with self.assertRaises(RuntimeError):
                    await cmts_cli.read_status(CMTS, MAC)
            writer.close.assert_called_once()

    async def test_error_after_ack_is_visible_instead_of_only_parse_error(self):
        reader, writer = self.connection(b"\0\1Access denied.\n")
        with (patch.object(cmts_cli, "_connect", AsyncMock(return_value=(reader, writer))),
              patch.object(cmts_cli, "_local_user", return_value="monitor")):
            with self.assertRaisesRegex(RuntimeError, "Access denied"):
                await cmts_cli.read_status(CMTS, MAC)

    async def test_early_disconnect_and_connection_failure_are_diagnostic(self):
        reader, writer = self.connection(b"")
        with (patch.object(cmts_cli, "_connect", AsyncMock(return_value=(reader, writer))),
              patch.object(cmts_cli, "_local_user", return_value="monitor")):
            with self.assertRaisesRegex(RuntimeError, "до подтверждения"):
                await cmts_cli.read_status(CMTS, MAC)
        with (patch.object(cmts_cli, "_connect", AsyncMock(side_effect=ConnectionRefusedError("refused"))),
              patch.object(cmts_cli, "_local_user", return_value="monitor")):
            with self.assertRaisesRegex(RuntimeError, "RSH: refused"):
                await cmts_cli.read_status(CMTS, MAC)

    async def test_reserved_source_port_is_bound_and_failed_socket_is_closed(self):
        import errno
        sock = Mock()
        sock.bind.side_effect = [OSError(errno.EADDRINUSE, "busy"), None]
        loop = Mock(sock_connect=AsyncMock())
        with (patch.object(cmts_cli.socket, "socket", return_value=sock),
              patch.object(cmts_cli.secrets, "randbelow", return_value=7),
              patch.object(cmts_cli.asyncio, "get_running_loop", return_value=loop),
              patch.object(cmts_cli.asyncio, "open_connection", AsyncMock(return_value=("reader", "writer")))):
            self.assertEqual(await cmts_cli._connect("192.0.2.1"), ("reader", "writer"))
        self.assertEqual(sock.bind.call_args_list[0].args, (("0.0.0.0", 519),))
        self.assertEqual(sock.bind.call_args_list[1].args, (("0.0.0.0", 520),))
        sock.close.assert_called_once()
        loop.sock_connect.assert_awaited_once_with(sock, ("192.0.2.1", 514))

    async def test_real_tcp_server_receives_zero_callback_and_returns_exact_state(self):
        received = asyncio.get_running_loop().create_future()

        async def handler(reader, writer):
            try:
                fields = [await reader.readuntil(b"\0") for _ in range(4)]
                received.set_result(fields)
                writer.write(b"\0" + b"0025.2e52.d8a6 10.190.0.177 C8/0/4/UB p-online 53\n")
                await writer.drain()
            except Exception as exc:
                if not received.done():
                    received.set_exception(exc)
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async def connect(address):
            return await asyncio.open_connection("127.0.0.1", port)
        try:
            with (patch.object(cmts_cli, "_connect", side_effect=connect),
                  patch.object(cmts_cli, "_local_user", return_value="monitor")):
                self.assertEqual(await cmts_cli.read_status(CMTS, MAC, "maksim"), "p-online")
            self.assertEqual(await received, [b"0\0", b"monitor\0", b"maksim\0",
                                               b"show cable modem 0025.2e52.d8a6\0"])
        finally:
            server.close()
            await server.wait_closed()


if __name__ == "__main__":
    unittest.main()
