"""Find DCN switches by SNMPv2c and optionally add/save ACL 6 over Telnet.

Install requirements-dcn-acl.txt. Edit credentials below or set DCN_TELNET_*
environment variables. Community is loaded from local .env.dcn_acl.
Without --apply no Telnet sessions or configuration writes are made.
"""
import argparse
import asyncio
import ipaddress
import json
import os
from pathlib import Path
import re
import sys

from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name('.env.dcn_acl'))

# Fill these in locally. Do not commit this file after adding real passwords.
TELNET_USERNAME = os.getenv('DCN_TELNET_USERNAME', '')
TELNET_PASSWORD = os.getenv('DCN_TELNET_PASSWORD', '')
ENABLE_PASSWORD = os.getenv('DCN_ENABLE_PASSWORD', '')
SNMP_COMMUNITY = os.getenv('DCN_SNMP_COMMUNITY', '')
CONFIG_COMMAND = 'config'
SAVE_COMMAND = 'write'
SHOW_RUNNING = 'show running-config'
SHOW_STARTUP = 'show startup-config'

PROMPT = re.compile(r'(?m)^[\w.\-/:]+(?:\([^\r\n]*\))?[>#]\s*\Z')
USER = re.compile(r'(?i)(?:user\s*name|login)\s*:\s*\Z')
PASSWORD = re.compile(r'(?i)password\s*:\s*\Z')
CONFIRM = re.compile(r'(?i)(?:\[\s*y(?:es)?\s*/\s*n(?:o)?\s*\]|\(\s*y/n\s*\)|\[confirm\])\s*[:?]?\s*\Z')
PAGER = re.compile(r'(?i)(?:--\s*more\s*--|press (?:any key|space)[^\r\n]*)[ \x08]*')
ERROR = re.compile(r'(?im)^\s*%|\b(?:invalid input|unknown command|incomplete command|ambiguous command|access denied|permission denied|authentication failed|authorization failed|login incorrect|save failed|write failed|error:)\b')
ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')


def acl_command(network):
    net = ipaddress.IPv4Network(network, strict=True)
    return f'access-list 6 permit {net.network_address} {net.hostmask}'


def has_acl(output, command):
    return any(' '.join(line.split()) == command for line in output.splitlines())


def is_switch_description(description):
    # The DCN enterprise also contains wireless equipment. Require a switch
    # model designation (S4600, DCS-3950, etc.) or explicit switch description.
    return bool(re.search(r'\b(?:(?:DCS[- ]?|S)\d{3,5}[\w-]*|switch)\b',
                          description, re.IGNORECASE))


def scan_networks(values, max_hosts):
    networks = list(ipaddress.collapse_addresses(
        ipaddress.IPv4Network(value, strict=False) for value in values))
    count = sum(n.num_addresses if n.prefixlen >= 31 else n.num_addresses - 2
                for n in networks)
    if count > max_hosts:
        raise ValueError(f'{count} addresses exceeds --max-hosts {max_hosts}')
    return networks, count


def configured_networks():
    return [value for value in re.split(r'[\s,;]+', os.getenv('DCN_SCAN_NETWORKS', '').strip())
            if value]


async def discover(networks, community, workers, timeout, snmp_port):
    from pysnmp.hlapi.v3arch.asyncio import (
        SnmpEngine, CommunityData, UdpTransportTarget, ContextData,
        ObjectType, ObjectIdentity, get_cmd,
    )
    from pysnmp.proto.rfc1905 import NoSuchObject, NoSuchInstance
    queue = asyncio.Queue(maxsize=workers * 2)
    found = []
    statistics = {'scanned': 0, 'no_response': 0, 'other_vendor': 0,
                  'not_identified_as_switch': 0, 'snmp_error': 0}

    async def worker():
        engine = SnmpEngine()
        try:
            while True:
                address = await queue.get()
                try:
                    if address is None:
                        return
                    statistics['scanned'] += 1
                    target = await UdpTransportTarget.create(
                        (address, snmp_port), timeout=timeout, retries=0)
                    err, status, _, bindings = await get_cmd(
                        engine, CommunityData(community, mpModel=1), target, ContextData(),
                        ObjectType(ObjectIdentity('1.3.6.1.2.1.1.2.0')),
                        ObjectType(ObjectIdentity('1.3.6.1.2.1.1.1.0')), lookupMib=False)
                    if err:
                        statistics['no_response'] += 1
                        continue
                    if status or len(bindings) != 2 or any(
                            isinstance(v, (NoSuchObject, NoSuchInstance)) for _, v in bindings):
                        statistics['snmp_error'] += 1
                        continue
                    oid = str(bindings[0][1]).lstrip('.')
                    # Enterprise 6339 is DCN. Do not rely on a configurable name alone.
                    if not (oid == '1.3.6.1.4.1.6339' or oid.startswith('1.3.6.1.4.1.6339.')):
                        statistics['other_vendor'] += 1
                        continue
                    value = bindings[1][1]
                    descr = value.asOctets().decode('utf-8', 'replace')
                    if not is_switch_description(descr):
                        statistics['not_identified_as_switch'] += 1
                        continue
                    record = {'ip': address, 'sys_object_id': oid,
                              'description': ' '.join(descr.split())[:500]}
                    found.append(record)
                    print(f'DCN {address}: {record["description"]}', flush=True)
                except Exception:
                    # A malformed response must not kill a worker and leave
                    # the bounded producer queue blocked forever.
                    statistics['snmp_error'] += 1
                finally:
                    queue.task_done()
        finally:
            engine.close_dispatcher()

    tasks = [asyncio.create_task(worker()) for _ in range(workers)]
    try:
        for net in networks:
            for address in net.hosts():
                await queue.put(str(address))
        for _ in tasks:
            await queue.put(None)
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return sorted(found, key=lambda r: ipaddress.IPv4Address(r['ip'])), statistics


class CLI:
    def __init__(self, reader, writer, timeout):
        self.reader, self.writer, self.timeout = reader, writer, timeout

    def send(self, text):
        self.writer.write(text + '\r\n')

    async def read(self, login=False, confirmation=False):
        """Read until a complete prompt; handle paginated config and save prompts."""
        output = ''
        deadline = asyncio.get_running_loop().time() + self.timeout
        confirmations = 0
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError('CLI prompt timeout; command outcome may be unknown')
            try:
                chunk = await asyncio.wait_for(self.reader.read(4096), remaining)
            except asyncio.TimeoutError as exc:
                raise TimeoutError('CLI prompt timeout; command outcome may be unknown') from exc
            if not chunk:
                raise ConnectionError('Telnet connection closed; command outcome may be unknown')
            output = ANSI.sub('', output + chunk).replace('\r', '')
            if len(output) > 2_000_000:
                raise RuntimeError('CLI output limit exceeded')
            if PAGER.search(output):
                output = PAGER.sub('', output)
                self.writer.write(' ')
                continue
            # Remove backspaces after processing the pagination marker.
            output = re.sub(r'.\x08', '', output)
            if confirmation and CONFIRM.search(output):
                confirmations += 1
                if confirmations > 3:
                    raise RuntimeError('Repeated save confirmation')
                answer = '' if '[confirm]' in output.lower() else 'y'
                output = CONFIRM.sub('', output)
                self.send(answer)
                continue
            if login:
                if USER.search(output): return 'username', output
                if PASSWORD.search(output): return 'password', output
            prompt = PROMPT.search(output)
            if prompt:
                return prompt.group(0).strip(), output

    async def command(self, text, confirmation=False):
        self.send(text)
        prompt, output = await self.read(confirmation=confirmation)
        if ERROR.search(output):
            # Do not expose full running config or accidentally echoed secrets.
            raise RuntimeError(f'CLI rejected command: {text}')
        return prompt, output

    async def login(self, username, password, enable_password):
        state, _ = await self.read(login=True)
        used_user = used_password = False
        while state in ('username', 'password'):
            if state == 'username' and not used_user:
                self.send(username); used_user = True
            elif state == 'password' and not used_password:
                self.send(password); used_password = True
            else:
                raise RuntimeError('Telnet authentication failed')
            state, _ = await self.read(login=True)
        if state.endswith('>'):
            self.send('enable')
            state, _ = await self.read(login=True)
            if state == 'password':
                if not enable_password:
                    raise RuntimeError('Enable password required; set ENABLE_PASSWORD')
                self.send(enable_password)
                state, _ = await self.read(login=True)
        if not state.endswith('#') or '(' in state:
            raise RuntimeError('Privileged EXEC prompt not obtained')


async def configure(address, args, command):
    import telnetlib3
    reader, writer = await asyncio.wait_for(telnetlib3.open_connection(
        host=address, port=args.telnet_port, encoding='utf8',
        connect_minwait=0.1, connect_maxwait=1, cols=200, rows=50), args.cli_timeout)
    cli = CLI(reader, writer, args.cli_timeout)
    phase = 'login'
    try:
        await cli.login(TELNET_USERNAME, TELNET_PASSWORD, ENABLE_PASSWORD)
        phase = 'disable session pagination'
        await cli.command('terminal length 0')
        phase = 'read running config'
        _, running = await cli.command(SHOW_RUNNING)
        existed = has_acl(running, command)
        if not existed:
            phase = 'apply ACL (may be modified if connection fails)'
            prompt, _ = await cli.command(CONFIG_COMMAND)
            if '(config' not in prompt.lower():
                raise RuntimeError('Configuration mode not obtained')
            await cli.command(command)
            prompt, _ = await cli.command('end')
            if '(' in prompt:
                raise RuntimeError('Could not leave configuration mode')
            _, running = await cli.command(SHOW_RUNNING)
            if not has_acl(running, command):
                raise RuntimeError('ACL not found in running config after command')
        phase = 'save (running config contains ACL)'
        await cli.command(SAVE_COMMAND, confirmation=True)
        phase = 'verify startup config (running config contains ACL)'
        _, startup = await cli.command(SHOW_STARTUP)
        if not has_acl(startup, command):
            raise RuntimeError('ACL not found in startup config; save NOT verified')
        return 'saved_existing' if existed else 'added_and_saved'
    except Exception as exc:
        raise RuntimeError(f'{phase}: {exc}') from exc
    finally:
        writer.close()


async def run(args):
    networks, count = scan_networks(args.networks, args.max_hosts)
    command = acl_command(args.permit_network)
    if not SNMP_COMMUNITY:
        raise ValueError('Set DCN_SNMP_COMMUNITY in .env.dcn_acl')
    if args.apply:
        if not TELNET_USERNAME or not TELNET_PASSWORD:
            raise ValueError('Set TELNET_USERNAME and TELNET_PASSWORD in script or environment')
        import telnetlib3  # Fail before scanning if the optional dependency is missing.
    print(f'Scan: {count} IPs; mode={"APPLY" if args.apply else "DISCOVER ONLY"}; {command}')
    found, statistics = await discover(networks, SNMP_COMMUNITY, args.workers,
                                       args.snmp_timeout, args.snmp_port)
    results = []
    for device in found:
        result = dict(device, result='discovered')
        if args.apply:
            try:
                result['result'] = await configure(device['ip'], args, command)
            except Exception as exc:
                result['result'] = 'failed'
                result['error'] = str(exc)
                for secret in (TELNET_PASSWORD, ENABLE_PASSWORD, SNMP_COMMUNITY):
                    if secret: result['error'] = result['error'].replace(secret, '[REDACTED]')
            print(f'{device["ip"]}: {result["result"]} {result.get("error", "")}', flush=True)
        results.append(result)
    report = {'networks': [str(n) for n in networks], 'command': command,
              'apply': args.apply, 'statistics': statistics, 'devices': results}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'DCN found: {len(found)}. Report: {args.report}')
    return 1 if any(r['result'] == 'failed' for r in results) else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('networks', nargs='*', help='IPv4 CIDRs; default: DCN_SCAN_NETWORKS in .env.dcn_acl')
    parser.add_argument('--apply', action='store_true', help='Add ACL and save via Telnet')
    parser.add_argument('--permit-network', default='10.18.90.0/24')
    parser.add_argument('--workers', type=int, default=32, help='Concurrent SNMP workers')
    parser.add_argument('--max-hosts', type=int, default=65536)
    parser.add_argument('--snmp-timeout', type=float, default=1.5)
    parser.add_argument('--cli-timeout', type=float, default=60)
    parser.add_argument('--snmp-port', type=int, default=161)
    parser.add_argument('--telnet-port', type=int, default=23)
    parser.add_argument('--report', type=Path, default=Path('outputs/dcn_acl_report.json'))
    args = parser.parse_args()
    if not args.networks:
        args.networks = configured_networks()
    if not args.networks:
        parser.error('Specify networks as arguments or fill DCN_SCAN_NETWORKS in .env.dcn_acl')
    if not 1 <= args.workers <= 256 or min(args.snmp_timeout, args.cli_timeout) <= 0:
        parser.error('workers must be 1..256 and timeouts must be positive')
    if not all(1 <= p <= 65535 for p in (args.snmp_port, args.telnet_port)):
        parser.error('ports must be 1..65535')
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print('Interrupted; check switches already being configured.', file=sys.stderr)
        return 130
    except (ValueError, OSError, ImportError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
