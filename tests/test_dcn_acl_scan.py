"""Offline DCN CLI simulations: no real switch connections."""
import asyncio
from argparse import Namespace
import sys
import types
import unittest
from unittest.mock import patch

import dcn_acl_scan as scan


class Reader:
    def __init__(self, chunks):
        self.chunks = iter(chunks)

    async def read(self, size):
        return next(self.chunks, '')


class Writer:
    def __init__(self):
        self.sent = []
        self.closed = False

    def write(self, data):
        self.sent.append(data)

    def close(self):
        self.closed = True


class ScanTests(unittest.IsolatedAsyncioTestCase):
    def test_networks_from_environment(self):
        with patch.dict(scan.os.environ, {'DCN_SCAN_NETWORKS': '10.0.0.0/24, 10.1.0.0/24;10.2.0.1'}):
            self.assertEqual(scan.configured_networks(),
                             ['10.0.0.0/24', '10.1.0.0/24', '10.2.0.1'])

    def test_switch_detection_excludes_wireless(self):
        self.assertTrue(scan.is_switch_description('S4600-28X-SI Device'))
        self.assertTrue(scan.is_switch_description('DCS-3950 series Ethernet switch'))
        self.assertFalse(scan.is_switch_description('DCN Wireless Access Point'))

    def test_networks_and_command(self):
        nets, count = scan.scan_networks(['10.0.0.0/24', '10.0.0.8/32'], 300)
        self.assertEqual(count, 254)
        self.assertEqual(len(nets), 1)
        self.assertEqual(scan.acl_command('10.18.90.0/24'),
                         'access-list 6 permit 10.18.90.0 0.0.0.255')
        with self.assertRaises(ValueError):
            scan.acl_command('10.18.90.0.0/24')
        with self.assertRaises(ValueError):
            scan.scan_networks(['10.0.0.0/8'], 65536)

    async def session(self, chunks):
        writer = Writer()
        chunks = list(chunks)
        for index, chunk in enumerate(chunks):
            if scan.PROMPT.search(chunk.replace('\r', '')):
                chunks.insert(index + 1, '\nswitch#')  # terminal length 0 response
                break

        async def connect(**kwargs):
            return Reader(chunks), writer

        module = types.SimpleNamespace(open_connection=connect)
        args = Namespace(telnet_port=23, cli_timeout=1)
        with patch.dict(sys.modules, {'telnetlib3': module}), \
                patch.object(scan, 'TELNET_USERNAME', 'operator'), \
                patch.object(scan, 'TELNET_PASSWORD', 'password'):
            try:
                result = await scan.configure('192.0.2.1', args, scan.acl_command('10.18.90.0/24'))
                return result, writer
            except Exception:
                self.last_writer = writer
                raise

    async def test_apply_paginated_verify_and_save_confirmation(self):
        command = scan.acl_command('10.18.90.0/24')
        result, writer = await self.session([
            'Username:', 'Password:', '\r\nswitch#',
            'show running-config\r\nother config\r\n--More--', '\r\nswitch#',
            '\r\nswitch(Config)#', '\r\nswitch(Config)#', '\r\nswitch#',
            f'\r\n{command}\r\nswitch#',
            'Save configuration? [Y/N]', '\r\nOK\r\nswitch#',
            f'\r\n{command}\r\nswitch#',
        ])
        self.assertEqual(result, 'added_and_saved')
        self.assertIn(command + '\r\n', writer.sent)
        self.assertIn('y\r\n', writer.sent)
        self.assertIn(' ', writer.sent)
        self.assertTrue(writer.closed)

    async def test_existing_entry_is_not_added_twice(self):
        command = scan.acl_command('10.18.90.0/24')
        result, writer = await self.session([
            'switch#', f'\n{command}\nswitch#', '\nOK\nswitch#',
            f'\n{command}\nswitch#',
        ])
        self.assertEqual(result, 'saved_existing')
        self.assertNotIn(command + '\r\n', writer.sent)

    async def test_cli_rejection_does_not_save(self):
        with self.assertRaisesRegex(RuntimeError, 'CLI rejected'):
            await self.session(['switch#', '\nswitch#', '\nswitch(Config)#',
                                '\n% Invalid input\nswitch(Config)#'])
        self.assertNotIn('write\r\n', self.last_writer.sent)
        self.assertTrue(self.last_writer.closed)

    async def test_save_not_verified_is_failure(self):
        command = scan.acl_command('10.18.90.0/24')
        with self.assertRaisesRegex(RuntimeError, 'save NOT verified'):
            await self.session(['switch#', f'\n{command}\nswitch#',
                                '\nOK\nswitch#', '\nother config\nswitch#'])

    async def test_authentication_retry_stops(self):
        with self.assertRaisesRegex(RuntimeError, 'authentication failed'):
            await self.session(['Username:', 'Password:', 'Username:'])

    async def test_prompt_timeout(self):
        class SlowReader:
            async def read(self, size):
                await asyncio.sleep(10)
        cli = scan.CLI(SlowReader(), Writer(), 0.01)
        with self.assertRaisesRegex(TimeoutError, 'CLI prompt timeout'):
            await cli.read()

    async def test_pager_with_backspaces(self):
        writer = Writer()
        cli = scan.CLI(Reader(['config line\n--More--\x08\x08', '\nnext line\nswitch#']),
                       writer, 1)
        prompt, output = await cli.read()
        self.assertEqual(prompt, 'switch#')
        self.assertEqual(writer.sent, [' '])
        self.assertIn('next line', output)


if __name__ == '__main__':
    unittest.main()
