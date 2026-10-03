"""Shared five-second IF-MIB polling for physical switch ports."""
import asyncio
import hashlib
import ipaddress
import threading
import time

from pysnmp.hlapi.v3arch.asyncio import (
    CommunityData, ContextData, ObjectIdentity, ObjectType, SnmpEngine,
    Udp6TransportTarget, UdpTransportTarget, bulk_walk_cmd,
)
from pysnmp.proto.rfc1905 import EndOfMibView, NoSuchInstance, NoSuchObject

from ping_monitor import validate_target

OID = {
    "ifDescr": "1.3.6.1.2.1.2.2.1.2",
    "ifType": "1.3.6.1.2.1.2.2.1.3",
    "ifSpeed": "1.3.6.1.2.1.2.2.1.5",
    "ifOperStatus": "1.3.6.1.2.1.2.2.1.8",
    "ifName": "1.3.6.1.2.1.31.1.1.1.1",
    "ifHighSpeed": "1.3.6.1.2.1.31.1.1.1.15",
    "ifConnectorPresent": "1.3.6.1.2.1.31.1.1.1.17",
}
ETHERNET_TYPES = {6, 62, 69, 117}
MAX_PORTS = 2048
CACHE_SECONDS = 4.5
MAX_ENTRIES = 80
_entries = {}
_entries_lock = threading.Lock()


def _number(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def build_ports(columns):
    """Join IF-MIB columns by ifIndex and keep physical Ethernet ports."""
    ports = []
    for index, raw_status in sorted(columns["ifOperStatus"].items()):
        connector = _number(columns["ifConnectorPresent"].get(index))
        interface_type = _number(columns["ifType"].get(index))
        if connector == 2 or (connector != 1 and interface_type not in ETHERNET_TYPES):
            continue
        name = (columns["ifName"].get(index) or columns["ifDescr"].get(index)
                or str(index)).strip()
        speed = _number(columns["ifHighSpeed"].get(index))
        if speed <= 0:
            speed = _number(columns["ifSpeed"].get(index)) // 1_000_000
        if _number(raw_status) != 1:
            state = "down"
        elif speed >= 1000:
            state = "fast"
        elif speed > 0:
            state = "slow"
        else:
            state = "unknown"
        ports.append({"index": index, "name": name[:100], "state": state,
                      "speed_mbps": speed if speed > 0 else None})
    return ports


async def poll_ports(address, port, community):
    """Walk standard IF-MIB columns directly on the switch via SNMPv2c."""
    if not validate_target(address) or not 1 <= int(port) <= 65535:
        raise ValueError("Нет корректного адреса или порта SNMP-интерфейса.")
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        ip = None
    transport_class = Udp6TransportTarget if ip and ip.version == 6 else UdpTransportTarget
    try:
        target = await transport_class.create((address, int(port)), timeout=1.5, retries=0)
        with SnmpEngine() as engine:
            async def walk(base):
                values = {}
                async for indication, status, _, bindings in bulk_walk_cmd(
                    engine, CommunityData(community, mpModel=1), target, ContextData(),
                    0, 25, ObjectType(ObjectIdentity(base)),
                    lexicographicMode=False, lookupMib=False, maxRows=MAX_PORTS,
                ):
                    if indication:
                        raise RuntimeError("SNMP: " + str(indication))
                    if status:
                        raise RuntimeError("SNMP: " + status.prettyPrint())
                    for binding in bindings:
                        oid = binding[0].prettyPrint()
                        if not oid.startswith(base + "."):
                            continue
                        suffix = oid[len(base) + 1:]
                        if not suffix.isdecimal() or isinstance(
                            binding[1], (NoSuchObject, NoSuchInstance, EndOfMibView)
                        ):
                            continue
                        values[int(suffix)] = binding[1].prettyPrint()
                return values

            names = ("ifDescr", "ifType", "ifSpeed", "ifOperStatus", "ifName",
                     "ifHighSpeed", "ifConnectorPresent")
            results = await asyncio.gather(*(walk(OID[name]) for name in names))
    except (OSError, ValueError) as exc:
        raise RuntimeError("Не удалось опросить SNMP: " + str(exc)) from exc
    columns = dict(zip(names, results))
    if not columns["ifOperStatus"]:
        raise RuntimeError("Коммутатор не возвращает ifOperStatus через IF-MIB.")
    return build_ports(columns)


class _Entry:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.last_seen = time.monotonic()
        self.updated = 0.0
        self.data = None


async def snapshot_for(host_id, address, port, community):
    """One SNMP poll per switch serves all viewers for about five seconds."""
    credential_id = hashlib.sha256(community.encode()).digest()
    key = (str(host_id), address, int(port), credential_id)
    with _entries_lock:
        now = time.monotonic()
        for old_key, old_entry in list(_entries.items()):
            if now - old_entry.last_seen > 60:
                del _entries[old_key]
        entry = _entries.get(key)
        if entry is None:
            if len(_entries) >= MAX_ENTRIES:
                raise RuntimeError("Слишком много активных SNMP-опросов.")
            entry = _Entry()
            _entries[key] = entry
        entry.last_seen = now
    async with entry.lock:
        if entry.data is not None and time.monotonic() - entry.updated < CACHE_SECONDS:
            return entry.data
        ports = await poll_ports(address, port, community)
        entry.data = {"ports": ports, "updated_at": int(time.time())}
        entry.updated = time.monotonic()
        return entry.data
