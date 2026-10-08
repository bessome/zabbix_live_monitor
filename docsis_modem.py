"""Basic live DOCSIS metrics for a modem not yet present in Zabbix."""
import asyncio
import hashlib
import time

from pysnmp.hlapi.v3arch.asyncio import (
    CommunityData, ContextData, ObjectIdentity, ObjectType, SnmpEngine,
    UdpTransportTarget, bulk_walk_cmd,
)
from pysnmp.proto.rfc1905 import EndOfMibView, NoSuchInstance, NoSuchObject

# DOCSIS-IF-MIB: modem upstream transmit level, downstream receive level and SNR.
COLUMNS = (
    ("US Level", "1.3.6.1.2.1.10.127.1.2.2.1.3", "dBmV"),
    ("DS Level", "1.3.6.1.2.1.10.127.1.1.1.1.6", "dBmV"),
    ("DS SNR", "1.3.6.1.2.1.10.127.1.1.4.1.5", "dB"),
)
_INVALID = (EndOfMibView, NoSuchInstance, NoSuchObject)


class _Entry:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.updated = 0.0
        self.last_seen = time.monotonic()
        self.data = None
        self.error = None


_entries = {}


async def basic_channels(address, community):
    key = (address, hashlib.sha256(community.encode()).digest())
    now = time.monotonic()
    for old_key, entry in list(_entries.items()):
        if now - entry.last_seen > 60:
            del _entries[old_key]
    entry = _entries.get(key)
    if entry is None:
        if len(_entries) >= 80:
            raise RuntimeError("Слишком много активных SNMP-опросов модемов.")
        entry = _Entry()
        _entries[key] = entry
    entry.last_seen = now
    async with entry.lock:
        if entry.updated and time.monotonic() - entry.updated < 4.5:
            if entry.error:
                raise RuntimeError(entry.error)
            return entry.data
        try:
            entry.data = await _read_channels(address, community)
            entry.error = None
        except (RuntimeError, OSError, ValueError) as exc:
            entry.data = None
            entry.error = str(exc)
        entry.updated = time.monotonic()
        if entry.error:
            raise RuntimeError(entry.error)
        return entry.data


async def _read_channels(address, community):
    """Read current values directly from the modem; no Zabbix item IDs exist."""
    target = await UdpTransportTarget.create((address, 161), timeout=3.0, retries=0)
    async def walk(engine, label, base, units):
        rows = []
        async for indication, status, _, bindings in bulk_walk_cmd(
            engine, CommunityData(community, mpModel=1), target, ContextData(),
            0, 10, ObjectType(ObjectIdentity(base)), lexicographicMode=False,
            lookupMib=False, maxRows=32,
        ):
            if indication:
                raise RuntimeError("SNMP: " + str(indication))
            if status:
                raise RuntimeError("SNMP: " + status.prettyPrint())
            for oid, value in bindings:
                oid_text = oid.prettyPrint()
                if not oid_text.startswith(base + ".") or isinstance(value, _INVALID):
                    continue
                try:
                    number = int(value)
                except (TypeError, ValueError):
                    continue
                index = oid_text[len(base) + 1:]
                rows.append({"id": label.replace(" ", "-") + "-" + index,
                             "label": label, "value": f"{number / 10:g}",
                             "units": units})
        if len(rows) > 1:
            for position, row in enumerate(rows, 1):
                row["label"] = row["label"].replace(" ", str(position) + " ", 1)
        return rows
    with SnmpEngine() as engine:
        groups = await asyncio.gather(
            *(walk(engine, label, base, units) for label, base, units in COLUMNS),
            return_exceptions=True,
        )
    items = []
    errors = []
    for group in groups:
        if isinstance(group, Exception):
            errors.append(str(group))
        else:
            items.extend(group)
    if errors and not items:
        raise RuntimeError(errors[0])
    return {"items": items, "updated_at": int(time.time())}
