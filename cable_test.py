"""TP-Link copper cable diagnostics through its private SNMP MIB."""
import asyncio
import ipaddress
import re
import threading

from pysnmp.hlapi.v3arch.asyncio import (
    CommunityData, ContextData, ObjectIdentity, ObjectType, SnmpEngine,
    Udp6TransportTarget, UdpTransportTarget, get_cmd, set_cmd,
)
from pysnmp.proto.rfc1902 import OctetString
from pysnmp.proto.rfc1905 import EndOfMibView, NoSuchInstance, NoSuchObject

from ping_monitor import validate_target

FLUSH_OID = "1.3.6.1.4.1.11863.6.8.1.3.1.0"
RESULT_BASE = "1.3.6.1.4.1.11863.6.8.1.3.2"
SYS_OBJECT_OID = "1.3.6.1.2.1.1.2.0"
PAIR_NAMES = "ABCD"
_running = set()
_running_lock = threading.Lock()


class CableTestBusy(Exception):
    """Another cable test is already running on this switch."""


def port_identifier(name):
    """Only the TP-Link 1/0/N layout has a documented result-table index."""
    match = re.search(r"(?:^|[^\d])1/0/([1-9]\d{0,3})$", name.strip(), re.I)
    if not match or int(match.group(1)) > 1024:
        raise ValueError("Тест кабеля поддерживается для портов TP-Link вида 1/0/N.")
    number = int(match.group(1))
    return f"1/0/{number}", 49152 + number


def decode_pairs(bindings, table_index):
    """Read status and length/fault location for pairs A-D."""
    values = {}
    for oid, value in bindings:
        if isinstance(value, (NoSuchObject, NoSuchInstance, EndOfMibView)):
            continue
        prefix = RESULT_BASE + "."
        suffix = f".{table_index}"
        name = oid.prettyPrint()
        if not name.startswith(prefix) or not name.endswith(suffix):
            continue
        column = name[len(prefix):-len(suffix)]
        if column.isdecimal():
            values[int(column)] = value.prettyPrint().strip()
    pairs = []
    for offset, pair in enumerate(PAIR_NAMES):
        status = values.get(2 + 2 * offset)
        distance = values.get(3 + 2 * offset)
        if not status and not distance:
            continue
        pairs.append({"pair": "Pair-" + pair, "status": status or "—",
                      "length": distance or "—", "error": "—"})
    return pairs


async def run_cable_test(host_id, address, port, read_community,
                         write_community, interface_name):
    """Start one test, wait five seconds, then read only that port's result."""
    port_id, table_index = port_identifier(interface_name)
    if not validate_target(address) or not 1 <= int(port) <= 65535:
        raise ValueError("Нет корректного адреса или порта SNMP-интерфейса.")
    with _running_lock:
        if host_id in _running:
            raise CableTestBusy()
        _running.add(host_id)
    try:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            ip = None
        transport_class = Udp6TransportTarget if ip and ip.version == 6 else UdpTransportTarget
        target = await transport_class.create((address, int(port)), timeout=3.0, retries=0)
        with SnmpEngine() as engine:
            vendor_error, vendor_status, _, vendor_bindings = await get_cmd(
                engine, CommunityData(read_community, mpModel=1), target, ContextData(),
                ObjectType(ObjectIdentity(SYS_OBJECT_OID)), lookupMib=False,
            )
            if vendor_error or vendor_status:
                raise RuntimeError("Не удалось определить производителя коммутатора по SNMP.")
            vendor = vendor_bindings[0][1].prettyPrint()
            if not vendor.startswith("1.3.6.1.4.1.11863."):
                raise ValueError("Тест кабеля доступен только для коммутаторов TP-Link.")

            indication, status, _, _ = await set_cmd(
                engine, CommunityData(write_community, mpModel=1), target, ContextData(),
                ObjectType(ObjectIdentity(FLUSH_OID), OctetString(port_id)), lookupMib=False,
            )
            if indication or status:
                raise RuntimeError("Не удалось запустить тест кабеля: "
                                   + str(indication or status.prettyPrint()))

            await asyncio.sleep(5)
            columns = [f"{RESULT_BASE}.{column}.{table_index}" for column in range(2, 10)]
            indication, status, _, bindings = await get_cmd(
                engine, CommunityData(read_community, mpModel=1), target, ContextData(),
                *(ObjectType(ObjectIdentity(oid)) for oid in columns), lookupMib=False,
            )
            if indication or status:
                raise RuntimeError("Не удалось прочитать результат теста: "
                                   + str(indication or status.prettyPrint()))
        pairs = decode_pairs(bindings, table_index)
        if not pairs:
            raise RuntimeError("Коммутатор не вернул результаты теста для этого порта.")
        return {"port": port_id, "pairs": pairs}
    finally:
        with _running_lock:
            _running.discard(host_id)
