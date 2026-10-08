"""Cisco CMTS MAC lookup using the standard DOCSIS-IF-MIB."""
import asyncio
import ipaddress
import re

from pysnmp.hlapi.v3arch.asyncio import (
    CommunityData, ContextData, ObjectIdentity, ObjectType, SnmpEngine,
    Udp6TransportTarget, UdpTransportTarget, get_cmd, bulk_walk_cmd,
)
from pysnmp.proto.rfc1905 import EndOfMibView, NoSuchInstance, NoSuchObject

MAC_COLUMN = "1.3.6.1.2.1.10.127.1.3.3.1.2"
IP_COLUMN = "1.3.6.1.2.1.10.127.1.3.3.1.3"
MAC_POINTER = "1.3.6.1.2.1.10.127.1.3.7.1.2"
MAX_SCAN_ROWS = 25000
MAX_RESULTS = 100
SEARCH_TIMEOUT = 35
SNMP_TIMEOUT = 3.0
_INVALID = (EndOfMibView, NoSuchInstance, NoSuchObject)


def normalize_mac(value):
    """Accept Cisco dotted, colon, dash and plain hex MACs or fragments."""
    raw = str(value).strip()
    if not raw or re.search(r"[^0-9a-fA-F.:\s-]", raw):
        raise ValueError("Введите MAC или его часть: только шестнадцатеричные цифры и разделители.")
    compact = re.sub(r"[.:\s-]", "", raw).lower()
    if not 2 <= len(compact) <= 12:
        raise ValueError("Введите от 2 до 12 шестнадцатеричных цифр MAC.")
    return compact


def display_mac(compact):
    return ":".join(compact[i:i + 2] for i in range(0, 12, 2))


def mac_instance(compact):
    return ".".join(str(int(compact[i:i + 2], 16)) for i in range(0, 12, 2))


def _address(value):
    if isinstance(value, _INVALID):
        return None
    try:
        address = ipaddress.ip_address(value.prettyPrint())
        return str(address) if address.version == 4 and not address.is_unspecified else None
    except ValueError:
        return None


async def _get(engine, target, community, oids):
    indication, status, _, bindings = await get_cmd(
        engine, CommunityData(community, mpModel=1), target, ContextData(),
        *(ObjectType(ObjectIdentity(oid)) for oid in oids), lookupMib=False,
    )
    if indication:
        raise RuntimeError("SNMP: " + str(indication))
    if status:
        raise RuntimeError("SNMP: " + status.prettyPrint())
    return [binding[1] for binding in bindings]


async def _target(cmts):
    transport = (Udp6TransportTarget if ipaddress.ip_address(cmts["address"]).version == 6
                 else UdpTransportTarget)
    return await transport.create(
        (cmts["address"], cmts["port"]), timeout=SNMP_TIMEOUT, retries=0)


async def resolve_mac(cmts, mac):
    """Two targeted GETs; return None when the modem is absent on this CMTS."""
    compact = normalize_mac(mac)
    if len(compact) != 12:
        raise ValueError("Для открытия модема нужен полный MAC.")
    target = await _target(cmts)
    with SnmpEngine() as engine:
        pointer = (await _get(engine, target, cmts["community"],
                              [MAC_POINTER + "." + mac_instance(compact)]))[0]
        if isinstance(pointer, _INVALID):
            return None
        try:
            index = int(pointer)
        except (TypeError, ValueError):
            return None
        ip = _address((await _get(engine, target, cmts["community"],
                                  [IP_COLUMN + "." + str(index)]))[0])
    return {"cmts_id": cmts["id"], "cmts_name": cmts["name"],
            "mac": display_mac(compact), "mac_compact": compact, "ip": ip}


async def _scan(cmts, fragment):
    target = await _target(cmts)
    matches = []
    scanned = 0
    truncated = False
    with SnmpEngine() as engine:
        async for indication, status, _, bindings in bulk_walk_cmd(
            engine, CommunityData(cmts["community"], mpModel=1),
            target, ContextData(), 0, 25,
            ObjectType(ObjectIdentity(MAC_COLUMN)), lexicographicMode=False,
            lookupMib=False, maxRows=MAX_SCAN_ROWS,
        ):
            if indication:
                raise RuntimeError("SNMP: " + str(indication))
            if status:
                raise RuntimeError("SNMP: " + status.prettyPrint())
            for oid, value in bindings:
                oid_text = oid.prettyPrint()
                if not oid_text.startswith(MAC_COLUMN + "."):
                    continue
                scanned += 1
                try:
                    compact = bytes(value.asOctets()).hex()
                except (AttributeError, ValueError):
                    continue
                if len(compact) != 12 or fragment not in compact:
                    continue
                matches.append((compact, oid_text[len(MAC_COLUMN) + 1:]))
                if len(matches) >= MAX_RESULTS:
                    truncated = True
                    break
            if truncated:
                break
        if scanned >= MAX_SCAN_ROWS:
            truncated = True
        rows = []
        for start in range(0, len(matches), 16):
            chunk = matches[start:start + 16]
            values = await _get(engine, target, cmts["community"],
                                [IP_COLUMN + "." + index for _, index in chunk])
            for (compact, _), value in zip(chunk, values):
                rows.append({"cmts_id": cmts["id"], "cmts_name": cmts["name"],
                             "mac": display_mac(compact), "mac_compact": compact,
                             "ip": _address(value)})
    return rows, truncated


async def search_cmts(cmts, fragment):
    compact = normalize_mac(fragment)
    try:
        if len(compact) == 12:
            found = await asyncio.wait_for(resolve_mac(cmts, compact), SEARCH_TIMEOUT)
            return ([found] if found else []), False
        return await asyncio.wait_for(_scan(cmts, compact), SEARCH_TIMEOUT)
    except asyncio.TimeoutError as exc:
        raise RuntimeError("Превышено время поиска на CMTS.") from exc
