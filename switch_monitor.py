"""Shared five-second IF-MIB polling for physical switch ports."""
import asyncio
from collections import Counter
import hashlib
import ipaddress
import re
import threading
import time

from pysnmp.hlapi.v3arch.asyncio import (
    CommunityData, ContextData, ObjectIdentity, ObjectType, SnmpEngine,
    Udp6TransportTarget, UdpTransportTarget, bulk_walk_cmd,
)
from pysnmp.proto.rfc1905 import EndOfMibView, NoSuchInstance, NoSuchObject

from ping_monitor import validate_target
from snmp_monitor import poll_values

OID = {
    "ifDescr": "1.3.6.1.2.1.2.2.1.2",
    "ifType": "1.3.6.1.2.1.2.2.1.3",
    "ifSpeed": "1.3.6.1.2.1.2.2.1.5",
    "ifOperStatus": "1.3.6.1.2.1.2.2.1.8",
    "ifName": "1.3.6.1.2.1.31.1.1.1.1",
    "ifHighSpeed": "1.3.6.1.2.1.31.1.1.1.15",
    "ifConnectorPresent": "1.3.6.1.2.1.31.1.1.1.17",
}
TRAFFIC_OID = {
    "in_hc": "1.3.6.1.2.1.31.1.1.1.6",
    "out_hc": "1.3.6.1.2.1.31.1.1.1.10",
    "in_32": "1.3.6.1.2.1.2.2.1.10",
    "out_32": "1.3.6.1.2.1.2.2.1.16",
}
ETHERNET_TYPES = {6, 62, 69, 117}
DEFAULT_EXCLUDED_NAMES = "Vlan|AUX|Loop"
MAX_PORTS = 2048
CACHE_SECONDS = 4.5
MAX_ENTRIES = 80
TRAFFIC_CACHE_SECONDS = 9.5
MAX_TRAFFIC_ENTRIES = 400
_entries = {}
_traffic_entries = {}
_entries_lock = threading.Lock()


def _number(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def parse_excluded_names(value):
    """Parse case-insensitive name fragments separated by vertical bars."""
    value = value.strip()
    if value.startswith("(") and value.endswith(")"):
        value = value[1:-1]
    if not value:
        return ()
    parts = [part.strip() for part in value.split("|")]
    if (len(value) > 300 or len(parts) > 20 or any(
        not part or len(part) > 40 or "(" in part or ")" in part
        or any(ord(char) < 32 for char in part) for part in parts
    )):
        raise ValueError("Укажите до 20 фрагментов имени через |, не более 40 символов каждый.")
    return tuple(dict.fromkeys(part.casefold() for part in parts))


def build_ports(columns, excluded_names=()):
    """Join IF-MIB columns by ifIndex and keep physical Ethernet ports."""
    ports = []
    connectors = columns["ifConnectorPresent"]
    ethernet_indices = [index for index in columns["ifOperStatus"]
                        if _number(columns["ifType"].get(index)) in ETHERNET_TYPES]
    physical_count = sum(_number(connectors.get(index)) == 1
                         for index in ethernet_indices)
    use_connectors = physical_count * 2 > len(ethernet_indices)
    for index, raw_status in columns["ifOperStatus"].items():
        connector = _number(columns["ifConnectorPresent"].get(index))
        interface_type = _number(columns["ifType"].get(index))
        if (interface_type not in ETHERNET_TYPES or connector == 2
                or (use_connectors and connector != 1)):
            continue
        full_name = (str(columns["ifName"].get(index, "")) + " "
                     + str(columns["ifDescr"].get(index, ""))).casefold()
        if any(fragment in full_name for fragment in excluded_names):
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
    def natural_key(port):
        numbers = tuple(int(part) for part in re.findall(r"\d+", port["name"]))
        return (not bool(numbers), numbers[-1] if numbers else 0,
                numbers[:-1], port["name"].casefold(), port["index"])
    ports.sort(key=natural_key)
    numbers_by_index = {
        port["index"]: re.findall(r"\d+", port["name"]) for port in ports
    }
    base_labels = {
        port["index"]: (str(int(parts[-1])) if parts else str(port["index"]))
        for port in ports for parts in [numbers_by_index[port["index"]]]
    }
    duplicates = Counter(base_labels.values())
    used_labels = set()
    for port in ports:
        label = base_labels[port["index"]]
        parts = numbers_by_index[port["index"]]
        if duplicates[label] > 1 and len(parts) >= 2:
            label = f"{int(parts[0])}:{int(parts[-1])}"
        if label in used_labels:
            base = label
            suffix = 1
            while label in used_labels:
                label = f"{base}{chr(96 + suffix) if suffix <= 26 else suffix}"
                suffix += 1
        used_labels.add(label)
        port["label"] = label
    return ports


async def poll_ports(address, port, community, excluded_names=()):
    """Walk standard IF-MIB columns directly on the switch via SNMPv2c."""
    if not validate_target(address) or not 1 <= int(port) <= 65535:
        raise ValueError("Нет корректного адреса или порта SNMP-интерфейса.")
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        ip = None
    transport_class = Udp6TransportTarget if ip and ip.version == 6 else UdpTransportTarget
    try:
        target = await transport_class.create((address, int(port)), timeout=3.0, retries=0)
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
    return build_ports(columns, excluded_names)


class _Entry:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.last_seen = time.monotonic()
        self.updated = 0.0
        self.data = None


async def snapshot_for(host_id, address, port, community, excluded_names=()):
    """One SNMP poll per switch serves all viewers for about five seconds."""
    credential_id = hashlib.sha256(community.encode()).digest()
    key = (str(host_id), address, int(port), credential_id, tuple(excluded_names))
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
        ports = await poll_ports(address, port, community, excluded_names)
        entry.data = {"ports": ports, "updated_at": int(time.time())}
        entry.updated = time.monotonic()
        return entry.data


class _TrafficEntry:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.last_seen = time.monotonic()
        self.updated = 0.0
        self.previous = None
        self.data = None


def _counter(values, direction):
    for kind in ("hc", "32"):
        try:
            value = int(values[f"{direction}_{kind}"])
            if value >= 0:
                return value, kind
        except (KeyError, TypeError, ValueError):
            pass
    return None


def _bit_rate(current, previous, seconds):
    if current is None or previous is None or current[1] != previous[1]:
        return None
    delta = current[0] - previous[0]
    if delta < 0 and current[1] == "32" and previous[0] > 0xF0000000:
        delta += 2 ** 32
    if delta < 0 or seconds <= 0:
        return None
    return round(delta * 8 / seconds)


async def traffic_for(host_id, if_index, address, port, community):
    """Share two IF-MIB counter samples per port across viewers, every 10 s."""
    credential_id = hashlib.sha256(community.encode()).digest()
    key = (str(host_id), int(if_index), address, int(port), credential_id)
    with _entries_lock:
        now = time.monotonic()
        for old_key, old_entry in list(_traffic_entries.items()):
            if now - old_entry.last_seen > 60:
                del _traffic_entries[old_key]
        entry = _traffic_entries.get(key)
        if entry is None:
            if len(_traffic_entries) >= MAX_TRAFFIC_ENTRIES:
                raise RuntimeError("Слишком много активных SNMP-опросов портов.")
            entry = _TrafficEntry()
            _traffic_entries[key] = entry
        entry.last_seen = now
    async with entry.lock:
        if entry.data is not None and time.monotonic() - entry.updated < TRAFFIC_CACHE_SECONDS:
            return entry.data
        definitions = [{"id": name, "label": name,
                        "oid": f"{base}.{if_index}", "multiplier": "1", "units": ""}
                       for name, base in TRAFFIC_OID.items()]
        try:
            readings = await poll_values(address, port, community, definitions)
        except (RuntimeError, ValueError) as exc:
            entry.previous = None
            entry.data = {"down_bps": None, "up_bps": None, "error": str(exc)}
            entry.updated = time.monotonic()
            return entry.data
        measured = time.monotonic()
        values = {reading["id"]: reading["value"] for reading in readings}
        current = {direction: _counter(values, direction)
                   for direction in ("in", "out")}
        previous = entry.previous
        seconds = measured - previous[0] if previous else 0
        entry.data = {
            "down_bps": _bit_rate(current["out"], previous[1]["out"], seconds)
            if previous else None,
            "up_bps": _bit_rate(current["in"], previous[1]["in"], seconds)
            if previous else None,
        }
        if current["in"] is None and current["out"] is None:
            entry.data["error"] = "Счётчики трафика IF-MIB недоступны."
        entry.previous = (measured, current)
        entry.updated = measured
        return entry.data
