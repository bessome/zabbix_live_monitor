"""Direct SNMPv2c polling for device metrics."""
import asyncio
from decimal import Decimal, InvalidOperation
import hashlib
import ipaddress
import re
import threading
import time

from pysnmp.hlapi.v3arch.asyncio import (
    CommunityData, ContextData, ObjectIdentity, ObjectType, SnmpEngine,
    Udp6TransportTarget, UdpTransportTarget, get_cmd,
)
from pysnmp.proto.rfc1905 import EndOfMibView, NoSuchInstance, NoSuchObject

from ping_monitor import validate_target

POLL_SECONDS = 5
ERROR_RATE_SECONDS = 10
CACHE_SECONDS = 4.5
MAX_ENTRIES = 80
_oid_pattern = re.compile(r"^\.?\d+(?:\.\d+)+$")
_us_pattern = re.compile(r"^Upstream channel\s+(US\d*)\s+Level$", re.I)
_ds_pattern = re.compile(
    r"^Downstream channel\s+(\d+)\s+(\S+MHz)\s+(Level|SNR)$", re.I,
)
_ds_generic_pattern = re.compile(r"^Downstream channel\s+(Level|SNR)$", re.I)
_error_pattern = re.compile(
    r"^Downstream channel\s+(\d+|\{#SNMPINDEX\})(?:\s+(\S+MHz))?\s+ErrorRate$", re.I,
)
_entries = {}
_entries_lock = threading.Lock()


def channel_definition(item):
    """Map a Zabbix SNMP item to a compact label and numeric OID."""
    name = str(item.get("name", "")).strip()
    upstream = _us_pattern.fullmatch(name)
    downstream = _ds_pattern.fullmatch(name)
    downstream_generic = _ds_generic_pattern.fullmatch(name)
    error = _error_pattern.fullmatch(name)
    if upstream:
        channel_text = upstream.group(1)[2:]
        channel = int(channel_text) if channel_text else 0
        label = f"US{channel_text} Level"
        order = (0, channel, 0, 0)
        metric = "level"
        frequency = ""
    elif downstream:
        channel = int(downstream.group(1))
        frequency = downstream.group(2)
        metric = downstream.group(3).upper()
        display_metric = "Level" if metric == "LEVEL" else "SNR"
        label = f"DS{channel} {frequency} {display_metric}"
        order = (1, channel, 0 if metric == "LEVEL" else 1, frequency)
        metric = metric.casefold()
    elif downstream_generic:
        metric = downstream_generic.group(1).upper()
        label = "DS Level" if metric == "LEVEL" else "DS SNR"
        order = (1, 0, 0 if metric == "LEVEL" else 1, "")
        metric = metric.casefold()
        channel = None
        frequency = ""
    elif error:
        channel = int(error.group(1)) if error.group(1).isdecimal() else None
        frequency = error.group(2) or ""
        label = (f"DS{channel}" if channel is not None else "DS") + (f" {frequency}" if frequency else "") + " ErrorRate"
        order = (1, channel or 0, 2, frequency)
        metric = "error_rate"
    else:
        return None
    definition = snmp_item_definition(item, label, allow_rate=metric == "error_rate")
    if definition is not None:
        definition["order"] = order
        definition["metric"] = metric
        definition["channel"] = channel
        definition["frequency"] = frequency.casefold()
    return definition


def optical_definition(item):
    """Map the TV amplifier's Optical input power item to a direct SNMP read."""
    if str(item.get("name", "")).strip().casefold() != "optical input power":
        return None
    return snmp_item_definition(item, "Optical input power")


def snmp_item_definition(item, label, allow_rate=False):
    if str(item.get("type")) != "20" or str(item.get("status", "0")) != "0":
        return None
    oid = str(item.get("snmp_oid", "")).strip()
    if oid.lower().startswith("get[") and oid.endswith("]"):
        oid = oid[4:-1].strip()
    if not _oid_pattern.fullmatch(oid):
        return None
    multiplier = Decimal(1)
    rate = False
    try:
        for step in item.get("preprocessing", []):
            step_type = str(step.get("type"))
            if step_type == "10" and allow_rate and not rate:
                rate = True
                continue
            if step_type != "1":
                return None
            multiplier *= Decimal(str(step["params"]).strip())
    except (InvalidOperation, KeyError):
        return None
    return {
        "id": str(item["itemid"]),
        "label": label,
        "oid": oid.lstrip("."),
        "units": str(item.get("units", "")),
        "value_type": str(item.get("value_type", "0")),
        "multiplier": str(multiplier),
        "rate": rate,
    }


def link_error_rates(definitions):
    """Attach an ErrorRate item to the SNR item of the same channel."""
    snr = [item for item in definitions if item.get("metric") == "snr"]
    errors = [item for item in definitions if item.get("metric") == "error_rate"]
    for item in snr:
        item.pop("error_rate_id", None)
        if item["channel"] is None:
            # Single-channel modems may call the metric simply "Downstream channel SNR".
            # Pair it only when there is exactly one possible ErrorRate item.
            if len(snr) == 1 and len(errors) == 1:
                item["error_rate_id"] = errors[0]["id"]
            continue
        candidates = [error for error in errors if error["channel"] == item["channel"]]
        exact = [error for error in candidates
                 if error["frequency"] and error["frequency"] == item["frequency"]]
        if len(exact) == 1:
            item["error_rate_id"] = exact[0]["id"]
        elif not exact and sum(other["channel"] == item["channel"] for other in snr) == 1:
            generic = [error for error in candidates if not error["frequency"]]
            if len(generic) == 1:
                item["error_rate_id"] = generic[0]["id"]


def display_modem_values(definitions, values):
    """Fold hidden ErrorRate polling results into the matching SNR row."""
    by_id = {item["id"]: item for item in values}
    rows = []
    for definition in definitions:
        if definition.get("metric") == "error_rate":
            continue
        value = by_id.get(definition["id"])
        if value is None:
            value = {"id": definition["id"], "label": definition["label"],
                     "value": None, "units": definition["units"]}
        row = dict(value)
        error_id = definition.get("error_rate_id")
        if error_id:
            row["error_rate"] = by_id.get(error_id, {}).get("value")
        rows.append(row)
    return rows


def _format_value(value, multiplier):
    if isinstance(value, (NoSuchObject, NoSuchInstance, EndOfMibView)):
        return None
    try:
        number = Decimal(value.prettyPrint()) * Decimal(multiplier)
    except (InvalidOperation, ValueError, AttributeError):
        return None
    result = format(number, "f")
    return result.rstrip("0").rstrip(".") if "." in result else result


async def poll_values(address, port, community, definitions):
    """Send batched SNMP GET requests directly to the device."""
    if not validate_target(address) or not 1 <= int(port) <= 65535:
        raise ValueError("Нет корректного адреса или порта SNMP-интерфейса.")
    if not definitions:
        return []
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        ip = None
    transport_class = Udp6TransportTarget if ip and ip.version == 6 else UdpTransportTarget
    try:
        target = await transport_class.create((address, int(port)), timeout=3.0, retries=0)
        with SnmpEngine() as engine:
            async def batch(items):
                result = await get_cmd(
                    engine, CommunityData(community, mpModel=1), target, ContextData(),
                    *(ObjectType(ObjectIdentity(item["oid"])) for item in items),
                    lookupMib=False,
                )
                indication, status, _, bindings = result
                if indication:
                    raise RuntimeError("SNMP: " + str(indication))
                if status:
                    raise RuntimeError("SNMP: " + status.prettyPrint())
                return [
                    {
                        "id": item["id"],
                        "label": item["label"],
                        "value": _format_value(binding[1], item["multiplier"]),
                        "units": item["units"],
                    }
                    for item, binding in zip(items, bindings)
                ]
            chunks = [definitions[i:i + 16] for i in range(0, len(definitions), 16)]
            results = await asyncio.gather(*(batch(chunk) for chunk in chunks))
    except (OSError, ValueError) as exc:
        raise RuntimeError("Не удалось опросить SNMP: " + str(exc)) from exc
    return [item for result in results for item in result]


class _Entry:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.last_seen = time.monotonic()
        self.updated = 0.0
        self.error_updated = 0.0
        self.data = None
        self.previous_rate = {}


async def snapshot_for(host_id, address, port, community, definitions):
    """Share polling: ordinary metrics every 5 s, ErrorRate every 10 s."""
    signature = tuple((item["id"], item["oid"], item["multiplier"], item.get("rate"))
                      for item in definitions)
    credential_id = hashlib.sha256(community.encode()).digest()
    key = (str(host_id), address, int(port), credential_id, signature)
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
        now = time.monotonic()
        if entry.data is not None and now - entry.updated < CACHE_SECONDS:
            return entry.data
        error_due = entry.data is None or now - entry.error_updated >= ERROR_RATE_SECONDS
        selected = [item for item in definitions
                    if item.get("metric") != "error_rate" or error_due]
        if not selected:
            return entry.data
        values = await poll_values(address, port, community, selected)
        measured_at = time.monotonic()
        definitions_by_id = {item["id"]: item for item in selected}
        for item in values:
            definition = definitions_by_id.get(item["id"])
            if definition is None or not definition.get("rate"):
                continue
            raw = item["value"]
            previous = entry.previous_rate.get(item["id"])
            try:
                current = Decimal(raw)
            except (InvalidOperation, TypeError):
                item["value"] = None
                continue
            entry.previous_rate[item["id"]] = (current, measured_at)
            if previous is None or current < previous[0]:
                item["value"] = None
                continue
            seconds = Decimal(str(measured_at - previous[1]))
            rate = (current - previous[0]) / seconds if seconds > 0 else Decimal(0)
            formatted = format(rate.quantize(Decimal("0.01")), "f")
            item["value"] = formatted.rstrip("0").rstrip(".") if "." in formatted else formatted
        previous = {item["id"]: item for item in entry.data["items"]} if entry.data else {}
        previous.update((item["id"], item) for item in values)
        entry.data = {"items": [previous[item["id"]] for item in definitions
                                if item["id"] in previous],
                      "updated_at": int(time.time())}
        entry.updated = measured_at
        if error_due and any(item.get("metric") == "error_rate" for item in selected):
            entry.error_updated = measured_at
        return entry.data
