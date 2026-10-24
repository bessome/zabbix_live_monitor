"""Zabbix API access, device mapping and short-lived caches."""
import json
import math
import os
import re
import threading
import time
from decimal import Decimal, InvalidOperation
from urllib import error, request as urlrequest

from fastapi import HTTPException

from app_config import CATEGORIES
from app_storage import setting
from snmp_monitor import channel_definition, link_error_rates, optical_definition, snmp_item_definition

_cache = {}
_catalog_cache = {}
_modem_items_cache = {}
_modem_restarts_cache = {}
_modem_restarts_daily_cache = {}
_optical_items_cache = {}
_ping_loss_items_cache = {}
_olt_hosts_cache = None
_olt_items_cache = {}
_olt_live_cache = {}
_OLT_ITEM_MARKERS = ("ONU OPT RX", "OLT Status interface", "ONU Optical RX",
                     "ONU Opitcal RX Level")
_cache_lock = threading.Lock()
_search_letters = str.maketrans({"ä": "a", "ö": "o", "õ": "o", "ü": "u"})
_list_us_level = re.compile(r"^Upstream channel\s+US(\d*)\s+Level$", re.I)
_list_ds_level = re.compile(r"^Downstream channel(?:\s+(\d+)\s+\S+MHz)?\s+Level$", re.I)

def category_filter(category):
    if category not in CATEGORIES:
        raise HTTPException(404)
    mode = setting("filter_mode:" + category, "group")
    ids = [item.strip() for item in setting("filter_ids:" + category).split(",")
           if item.strip()]
    return mode, ids


def normalize_device_search(value):
    folded = value.casefold().translate(_search_letters)
    return " ".join(part for part in re.split(r"[-_\s]+", folded) if part)


def zabbix_call(method, params, timeout=8):
    url = setting("zabbix_url").strip()
    token = os.environ.get("ZABBIX_API_TOKEN", "")
    if not url or not token:
        raise RuntimeError("Укажите URL Zabbix и ZABBIX_API_TOKEN.")
    payload = json.dumps({"jsonrpc": "2.0", "method": method,
                          "params": params, "id": 1}).encode()
    api_request = urlrequest.Request(
        url, data=payload,
        headers={"Content-Type": "application/json-rpc",
                 "Authorization": "Bearer " + token})
    try:
        with urlrequest.urlopen(api_request, timeout=timeout) as response:
            answer = json.load(response)
    except (error.URLError, TimeoutError, ValueError) as exc:
        raise RuntimeError("Zabbix API недоступен: " + str(exc)) from exc
    if "error" in answer:
        raise RuntimeError("Zabbix API: " + answer["error"].get("message", "ошибка"))
    return answer["result"]


def host_rows(category):
    mode, ids = category_filter(category)
    if not ids:
        return []
    with _cache_lock:
        cached = _cache.get(category)
        if cached and time.monotonic() - cached[0] < 10:
            return cached[1]
    hosts = zabbix_call("host.get", {
        "output": ["hostid", "host", "name", "status"],
        "selectInterfaces": ["ip", "dns", "useip", "main", "available", "type", "port"],
        "selectHostGroups": ["groupid", "name"],
        "sortfield": "name", "sortorder": "ASC",
        "groupids" if mode == "group" else "templateids": ids})
    rows = []
    for host in hosts:
        interfaces = host.get("interfaces", [])
        primary = next((interface for interface in interfaces
                        if interface.get("main") == "1"), interfaces[0] if interfaces else {})
        address = (primary.get("dns") if primary.get("useip") == "0" else primary.get("ip"))
        address = address or primary.get("ip") or primary.get("dns") or "—"
        snmp_interfaces = [interface for interface in interfaces if interface.get("type") == "2"]
        snmp_interface = next((interface for interface in snmp_interfaces
                               if interface.get("main") == "1"),
                              snmp_interfaces[0] if snmp_interfaces else primary)
        snmp_address = (snmp_interface.get("dns") if snmp_interface.get("useip") == "0"
                        else snmp_interface.get("ip"))
        snmp_address = snmp_address or snmp_interface.get("ip") or snmp_interface.get("dns") or address
        snmp_port = snmp_interface.get("port") or "161"
        availability = ("available" if any(i.get("available") == "1" for i in interfaces)
                        else "unavailable" if any(i.get("available") == "2" for i in interfaces)
                        else "unknown")
        rows.append({"id": host["hostid"], "name": host["name"],
                      "technical_name": host["host"],
                       "address": address,
                       "interface_ip": primary.get("ip") or "",
                       "snmp_address": snmp_address, "snmp_port": snmp_port,
                      "enabled": host["status"] == "0", "availability": availability,
                      "groups": [group["name"] for group in host.get("hostgroups", [])
                                 if group.get("name")]})
    with _cache_lock:
        _cache[category] = (time.monotonic(), rows)
    return rows


def switch_pvid_items(host_id):
    """Read PVID items only from the already authorized switch."""
    return zabbix_call("item.get", {
        "hostids": [host_id],
        "output": ["itemid", "name", "key_", "snmp_oid", "lastvalue",
                   "lastclock", "status", "state"],
        "search": {"name": "pvid", "key_": "pvid"}, "searchByAny": True,
    })


def switch_port_pvid(items, if_index, interface_name, bridge_ports=None):
    """Prefer an exact interface name; never guess a bridge port from ifIndex."""
    name_pattern = re.compile(r"(?<![\w/.-])" + re.escape(interface_name)
                              + r"(?![\w/.-])", re.I)
    bridge_indices = {index[0] for index, value in (bridge_ports or {}).items()
                      if value == if_index}
    matches = []
    for item in items:
        name, key = item.get("name", ""), item.get("key_", "")
        if "pvid" not in (name + " " + key).casefold():
            continue
        score = 0
        if interface_name and any(name_pattern.search(text) for text in (name, key)):
            score = 2
        oid = item.get("snmp_oid", "").lstrip(".")
        prefix = "1.3.6.1.2.1.17.7.1.4.5.1.1."
        if oid.startswith(prefix) and oid[len(prefix):].isdecimal():
            if int(oid[len(prefix):]) in bridge_indices:
                score = max(score, 1)
        if score:
            matches.append((score, item))
    if not matches:
        return None
    best = max(score for score, _ in matches)
    selected = [item for score, item in matches if score == best]
    if len(selected) != 1:
        return None
    item = selected[0]
    if (str(item.get("status", "0")) != "0" or str(item.get("state", "0")) != "0"
            or str(item.get("lastclock", "0")) == "0"):
        return None
    try:
        value = int(item.get("lastvalue", ""))
    except (TypeError, ValueError):
        return None
    return value if 1 <= value <= 4094 else None


def device_descriptions(host_ids):
    """Read the latest Device description for the visible hosts in one API call."""
    if not host_ids:
        return {}
    items = zabbix_call("item.get", {
        "output": ["hostid", "lastvalue", "lastclock"],
        "hostids": host_ids,
        "filter": {"name": "Device description"},
    })
    descriptions = {}
    for item in items:
        if str(item.get("lastclock", "0")) != "0" and item.get("lastvalue"):
            descriptions[str(item["hostid"])] = item["lastvalue"]
    return descriptions


def modem_list_levels(host_ids, *, include_raw=False):
    """Read one current US and DS level per visible modem from Zabbix."""
    if not host_ids:
        return {}
    items = zabbix_call("item.get", {
        "output": ["itemid", "hostid", "name", "status", "value_type",
                   "units", "lastvalue", "lastclock"],
        "hostids": host_ids,
        "search": {"name": "Level"},
    })
    choices = {}
    for item in items:
        if (str(item.get("hostid")) not in host_ids
                or str(item.get("status")) != "0"
                or str(item.get("value_type")) not in ("0", "3")):
            continue
        us = _list_us_level.fullmatch(str(item.get("name", "")).strip())
        ds = _list_ds_level.fullmatch(str(item.get("name", "")).strip())
        if not us and not ds:
            continue
        try:
            clock = int(item.get("lastclock") or 0)
            value = Decimal(str(item["lastvalue"]))
        except (KeyError, TypeError, ValueError, InvalidOperation):
            continue
        if clock <= 0 or not value.is_finite():
            continue
        match = us or ds
        channel = int(match.group(1)) if match.group(1) else 9999
        kind = "us" if us else "ds"
        # Prefer a numbered channel over a generic item, then the lowest channel.
        rank = (channel, -clock, str(item.get("itemid", "")))
        key = (str(item["hostid"]), kind)
        if key not in choices or rank < choices[key][0]:
            rounded = value.quantize(Decimal("0.01"))
            if rounded == 0:
                rounded = Decimal(0)
            choices[key] = (rank, {"value": format(rounded.normalize(), "f"),
                                   "units": str(item.get("units") or ""),
                                   "alert": value < (20 if kind == "us" else -15)
                                   or value > (52 if kind == "us" else 18)})
            if include_raw:
                choices[key][1]["raw_value"] = str(value)
    result = {}
    for (host_id, kind), (_, metric) in choices.items():
        result.setdefault(host_id, {})[kind] = metric
    return result


def edfa_levels(host_ids):
    """Latest enabled numeric EDFA items; keep source precision for the UI."""
    def state_for(kind, number):
        if kind == "output":
            return "critical" if number <= 20 else "warning" if number < Decimal("22.7") else "normal"
        return "critical" if number < -1 else "normal"

    return _named_device_levels(host_ids, {"Optical output level": "output", "Optical input level": "input"}, state_for)


def ups_levels(host_ids):
    """UPS measurements from enabled numeric items, with original precision."""
    def state_for(kind, number):
        if kind == "battery":
            return "critical" if number < 50 else "warning" if number < 100 else "normal"
        return "unrated"

    return _named_device_levels(host_ids, {
        "Output Load L1": "load", "Battery Capacity": "battery", "Autonomy": "autonomy",
    }, state_for, case_insensitive=True)


def _named_device_levels(host_ids, labels, state_for, *, case_insensitive=False):
    if not host_ids:
        return {}
    names = {label.casefold(): kind for label, kind in labels.items()}
    kind_labels = {kind: label for label, kind in labels.items()}
    items = zabbix_call("item.get", {
        "output": ["itemid", "hostid", "name", "status", "value_type", "units", "lastvalue", "lastclock"],
        "hostids": host_ids,
        **({"search": {"name": list(labels)}, "searchByAny": True} if case_insensitive
           else {"filter": {"name": list(labels)}}),
    })
    choices = {}
    for item in items:
        host_id = str(item.get("hostid"))
        kind = names.get(str(item.get("name", "")).strip().casefold())
        if host_id not in host_ids or not kind or str(item.get("status")) != "0" or str(item.get("value_type")) not in ("0", "3"):
            continue
        try:
            clock = int(item.get("lastclock") or 0)
        except (TypeError, ValueError):
            clock = 0
        value = None
        try:
            number = Decimal(str(item.get("lastvalue")))
            if clock > 0 and number.is_finite():
                value = str(number)
        except InvalidOperation:
            pass
        rank = (-clock, str(item.get("itemid", "")))
        state = "unknown"
        if value is not None:
            state = state_for(kind, number)
        key = (host_id, kind)
        if key not in choices or rank < choices[key][0]:
            choices[key] = (rank, {"id": str(item["itemid"]), "label": kind_labels[kind],
                                   "value": value, "raw_value": value, "state": state, "units": str(item.get("units") or ""),
                                   "value_type": str(item["value_type"]), "lastclock": clock})
    result = {host_id: {} for host_id in host_ids}
    for (host_id, kind), (_, metric) in choices.items():
        result[host_id][kind] = metric
    return result


def amplifier_list_optical_levels(host_ids, *, include_raw=False):
    """Read the current Optical input power item for each visible amplifier."""
    if not host_ids:
        return {}
    items = zabbix_call("item.get", {
        "output": ["itemid", "hostid", "name", "status", "value_type",
                   "units", "lastvalue", "lastclock"],
        "hostids": host_ids,
        "filter": {"name": "Optical input power"},
    })
    choices = {}
    for item in items:
        host_id = str(item.get("hostid"))
        if (host_id not in host_ids or str(item.get("status")) != "0"
                or str(item.get("value_type")) not in ("0", "3")
                or str(item.get("name", "")).strip().casefold() != "optical input power"):
            continue
        try:
            clock = int(item.get("lastclock") or 0)
            value = Decimal(str(item["lastvalue"]))
        except (KeyError, TypeError, ValueError, InvalidOperation):
            continue
        if clock <= 0 or not value.is_finite():
            continue
        rank = (-clock, str(item.get("itemid", "")))
        if host_id not in choices or rank < choices[host_id][0]:
            rounded = value.quantize(Decimal("0.01"))
            if rounded == 0:
                rounded = Decimal(0)
            metric = {"value": format(rounded.normalize(), "f"),
                      "units": str(item.get("units") or ""),
                      "alert": value < Decimal("-3.4")}
            if include_raw:
                metric["raw_value"] = str(value)
            choices[host_id] = (rank, metric)
    return {host_id: metric for host_id, (_, metric) in choices.items()}


def modem_channel_definitions(host_id):
    with _cache_lock:
        cached = _modem_items_cache.get(host_id)
        if cached and time.monotonic() - cached[0] < 300:
            return cached[1]
    items = zabbix_call("item.get", {
        "output": ["itemid", "name", "type", "status", "snmp_oid", "units", "value_type"],
        "hostids": [host_id],
        "search": {"name": "channel"},
        "selectPreprocessing": "extend",
    })
    definitions = [definition for item in items
                   if (definition := channel_definition(item)) is not None]
    definitions.sort(key=lambda item: item["order"])
    link_error_rates(definitions)
    with _cache_lock:
        _modem_items_cache[host_id] = (time.monotonic(), definitions)
    return definitions


def modem_overview_definitions(definitions, loss_item):
    """Pick the first numbered upstream and DS3, or generic modem graph items."""
    by_id = {item["id"]: item for item in definitions}
    multi = any(item.get("channel") is not None
                and item.get("label", "").startswith("DS")
                and item.get("metric") in ("level", "snr")
                for item in definitions)
    if multi:
        upstream = min((item for item in definitions
                        if item.get("label", "").startswith("US")
                        and item.get("metric") == "level"
                        and isinstance(item.get("channel"), int)
                        and item["channel"] > 0),
                       key=lambda item: item["channel"], default=None)
        snr = next((item for item in definitions
                    if item.get("channel") == 3 and item.get("metric") == "snr"), None)
        levels = [item for item in definitions
                  if item.get("channel") == 3 and item.get("metric") == "level"]
        level = next((item for item in levels
                      if snr and item.get("frequency") == snr.get("frequency")),
                     levels[0] if levels else None)
        names = (upstream["label"] if upstream else "US Level",
                 "DS3 Level", "DS3 SNR", "DS3 ErrorRate")
    else:
        upstream = next((item for item in definitions
                         if item.get("label") == "US Level"), None)
        level = next((item for item in definitions
                      if item.get("label") == "DS Level"), None)
        snr = next((item for item in definitions
                    if item.get("label") == "DS SNR"), None)
        names = ("US Level", "DS Level", "DS SNR", "ErrorRate")
    error = by_id.get(snr.get("error_rate_id")) if snr else None
    selected = []
    missing = []
    for key, label, item in zip(("us", "ds_level", "ds_snr", "error_rate", "loss"),
                                (*names, "Loss"),
                                (upstream, level, snr, error, loss_item)):
        if item is None:
            missing.append(label)
        else:
            selected.append({"key": key, "label": label, "item": item})
    return "multi" if multi else "single", selected, missing


def modem_restarts_item(host_id):
    """Find the modem restarts item and read its latest Zabbix value."""
    with _cache_lock:
        cached = _modem_restarts_cache.get(host_id)
        if cached and time.monotonic() - cached[0] < 15:
            return cached[1]
    items = zabbix_call("item.get", {
        "output": ["itemid", "name", "status", "value_type", "units",
                   "lastvalue", "lastclock"],
        "hostids": [host_id],
        "search": {"name": "restarts count per hour"},
    })
    matches = [item for item in items
               if item.get("name", "").strip().casefold() == "restarts count per hour"
               and str(item.get("status")) == "0"
               and str(item.get("value_type")) in ("0", "3")]
    result = None
    if matches:
        selected = max(matches, key=lambda item: (int(item.get("lastclock") or 0),
                                                   int(item["itemid"])))
        value = None
        if int(selected.get("lastclock") or 0) > 0:
            try:
                parsed = float(selected["lastvalue"])
                if math.isfinite(parsed):
                    value = int(parsed) if parsed.is_integer() else round(parsed, 2)
            except (KeyError, TypeError, ValueError):
                pass
        result = {"id": str(selected["itemid"]), "label": "Рестарты/ч",
                  "units": selected.get("units") or "",
                  "value_type": selected["value_type"], "value": value}
    with _cache_lock:
        _modem_restarts_cache[host_id] = (time.monotonic(), result)
    return result


def modem_restarts_count_24h(host_id, item, now=None):
    """Sum one latest hourly count per rolling hour over the last 24 hours."""
    now = int(time.time()) if now is None else now
    cache_key = (host_id, item["id"])
    with _cache_lock:
        cached = _modem_restarts_daily_cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < 60:
            return cached[1]
    from_time = now - 86400
    cursor = now
    hourly = {}
    while cursor > from_time:
        page = zabbix_call("history.get", {
            "output": ["clock", "value"], "itemids": [item["id"]],
            "history": int(item["value_type"]),
            "time_from": from_time, "time_till": cursor,
            "sortfield": "clock", "sortorder": "DESC", "limit": 50000,
        })
        for row in page:
            try:
                timestamp = int(row["clock"])
                value = float(row["value"])
            except (KeyError, TypeError, ValueError):
                continue
            if from_time < timestamp <= now and math.isfinite(value):
                hourly.setdefault((now - timestamp) // 3600, (timestamp, max(0, value)))
        if len(page) < 50000:
            break
        cursor = int(page[-1]["clock"]) - 1
    count = round(sum(value for _, value in hourly.values())) if hourly else None
    with _cache_lock:
        _modem_restarts_daily_cache[cache_key] = (time.monotonic(), count)
    return count


def optical_power_definitions(host_id):
    with _cache_lock:
        cached = _optical_items_cache.get(host_id)
        if cached and time.monotonic() - cached[0] < 300:
            return cached[1]
    items = zabbix_call("item.get", {
        "output": ["itemid", "name", "type", "status", "snmp_oid", "units", "value_type"],
        "hostids": [host_id],
        "filter": {"name": "Optical input power"},
        "selectPreprocessing": "extend",
    })
    definitions = [definition for item in items
                   if (definition := optical_definition(item)) is not None]
    definitions.sort(key=lambda item: int(item["id"]))
    if len(definitions) > 1:
        for index, definition in enumerate(definitions, 1):
            definition["label"] += f" {index}"
    with _cache_lock:
        _optical_items_cache[host_id] = (time.monotonic(), definitions)
    return definitions


def ping_loss_definition(host_id):
    with _cache_lock:
        cached = _ping_loss_items_cache.get(host_id)
        if cached and time.monotonic() - cached[0] < 300:
            return cached[1]
    items = zabbix_call("item.get", {
        "output": ["itemid", "key_", "status", "value_type", "units"],
        "hostids": [host_id], "search": {"key_": "icmppingloss"},
    })
    matches = [item for item in items
               if (item.get("key_") == "icmppingloss"
                   or str(item.get("key_", "")).startswith("icmppingloss["))
               and str(item.get("status")) == "0"
               and str(item.get("value_type")) in ("0", "3")]
    matches.sort(key=lambda item: (not (item["key_"] == "icmppingloss"
                                        or item["key_"].startswith("icmppingloss[,")),
                                   len(item["key_"]), item["itemid"]))
    definition = ({"id": str(matches[0]["itemid"]), "label": "Потери пакетов Zabbix",
                   "units": matches[0].get("units") or "%",
                   "value_type": matches[0]["value_type"]} if matches else None)
    with _cache_lock:
        _ping_loss_items_cache[host_id] = (time.monotonic(), definition)
    return definition


def zabbix_catalog(kind):
    methods = {"group": ("hostgroup.get", "groupid"),
               "template": ("template.get", "templateid")}
    if kind not in methods:
        raise HTTPException(404)
    with _cache_lock:
        cached = _catalog_cache.get(kind)
        if cached and time.monotonic() - cached[0] < 60:
            return cached[1]
    method, id_field = methods[kind]
    result = zabbix_call(method, {"output": [id_field, "name"],
                                  "sortfield": "name", "sortorder": "ASC"})
    items = [{"id": str(row[id_field]), "name": row["name"]} for row in result]
    items.sort(key=lambda item: item["name"].casefold())
    with _cache_lock:
        _catalog_cache[kind] = (time.monotonic(), items)
    return items


def olt_hosts():
    """OLT hosts in the Zabbix group linked from the ONU/ONT section."""
    global _olt_hosts_cache
    with _cache_lock:
        cached = _olt_hosts_cache
        if cached and time.monotonic() - cached[0] < 60:
            return cached[1]
    hosts = zabbix_call("host.get", {
        "output": ["hostid", "host", "name"], "groupids": ["100"],
        "sortfield": "name", "sortorder": "ASC",
    })
    rows = [{"id": str(host["hostid"]), "name": host["name"],
             "technical_name": host.get("host") or ""} for host in hosts]
    rows.sort(key=lambda row: row["name"].casefold())
    with _cache_lock:
        _olt_hosts_cache = (time.monotonic(), rows)
    return rows


def _compact_olt_text(value):
    return re.sub(r"[^0-9a-z]", "", value.casefold())


def normalize_olt_item_search(value):
    """Accept a partial hexadecimal MAC with optional separators."""
    compact = _compact_olt_text(value)
    return compact if re.fullmatch(r"[0-9a-f]+", compact) else ""


def is_olt_target_item(name):
    folded = " ".join(name.casefold().split())
    return any(marker.casefold() in folded for marker in _OLT_ITEM_MARKERS)


def olt_items(host_ids, query, limit=100):
    """Find the ONU/OLT optical and status items by a partial MAC."""
    if not host_ids:
        return [], False
    needle = normalize_olt_item_search(query)
    if len(needle) < 2:
        return [], False
    cache_key = tuple(sorted(host_ids))
    with _cache_lock:
        cached = _olt_items_cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < 30:
            candidates = cached[1]
        else:
            candidates = None
    if candidates is None:
        by_id = {}
        for marker in _OLT_ITEM_MARKERS:
            items = zabbix_call("item.get", {
                "output": ["itemid", "hostid", "name", "key_", "status",
                           "value_type", "units", "lastvalue", "lastclock"],
                "hostids": host_ids, "filter": {"status": "0"},
                "search": {"name": marker},
            }, timeout=30)
            for item in items:
                if is_olt_target_item(item.get("name", "")):
                    by_id[str(item["itemid"])] = item
        candidates = list(by_id.values())
        with _cache_lock:
            _olt_items_cache[cache_key] = (time.monotonic(), candidates)
    matches = [item for item in candidates
               if needle in _compact_olt_text(item.get("name", ""))
               or needle in _compact_olt_text(item.get("key_", ""))]
    matches.sort(key=lambda item: (item.get("name", "").casefold(), str(item["itemid"])))
    return matches[:limit], len(matches) > limit


_olt_serial_pattern = re.compile(
    r"(?<![0-9a-f])(?:[0-9a-f]{2}[\s:-]?){5}[0-9a-f]{2}(?![0-9a-f])", re.I)


def olt_item_serial(name):
    match = _olt_serial_pattern.search(name)
    return re.sub(r"[^0-9a-f]", "", match.group().casefold()) if match else None


def olt_live_config(host_id, item_id):
    """Resolve exact ONU items and their own Zabbix OIDs."""
    cache_key = (str(host_id), str(item_id))
    with _cache_lock:
        cached = _olt_live_cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < 60:
            return cached[1]
    selected = zabbix_call("item.get", {
        "output": ["itemid", "hostid", "name", "status"],
        "hostids": [host_id], "itemids": [item_id],
    })
    clicked = next((row for row in selected
                    if str(row.get("itemid")) == str(item_id)
                    and str(row.get("hostid")) == str(host_id)
                    and str(row.get("status")) == "0"
                    and is_olt_target_item(row.get("name", ""))), None)
    serial = olt_item_serial(clicked["name"]) if clicked else None
    if serial is None:
        return None
    candidates, _ = olt_items([host_id], serial, limit=1000)
    ids = [str(row["itemid"]) for row in candidates
           if str(row.get("hostid")) == str(host_id)
           and olt_item_serial(row.get("name", "")) == serial]
    if str(item_id) not in ids:
        ids.append(str(item_id))
    items = zabbix_call("item.get", {
        "output": ["itemid", "hostid", "name", "type", "status", "snmp_oid",
                   "interfaceid", "units", "value_type"],
        "hostids": [host_id], "itemids": ids,
        "selectPreprocessing": "extend",
    })
    valid = [row for row in items if str(row.get("hostid")) == str(host_id)
             and str(row.get("status")) == "0"
             and is_olt_target_item(row.get("name", ""))
             and olt_item_serial(row.get("name", "")) == serial]
    definitions = [(row, snmp_item_definition(row, row["name"])) for row in valid]
    interface_ids = sorted({str(row.get("interfaceid")) for row, definition in definitions
                            if definition and row.get("interfaceid")})
    interfaces = zabbix_call("hostinterface.get", {
        "output": "extend", "interfaceids": interface_ids,
    }) if interface_ids else []
    by_interface = {str(row["interfaceid"]): row for row in interfaces
                    if str(row.get("hostid")) == str(host_id) and str(row.get("type")) == "2"}
    groups = {}
    rows = []
    for item, definition in definitions:
        interface = by_interface.get(str(item.get("interfaceid")))
        details = (interface.get("details") or {}) if interface else {}
        community = str(details.get("community") or "").strip()
        if not community or community.startswith("{$"):
            community = os.environ.get("ONU_SNMP_COMMUNITY", "").strip()
        address = ((interface.get("dns") if str(interface.get("useip")) == "0"
                    else interface.get("ip")) if interface else None)
        port = str(interface.get("port") or "161") if interface else "161"
        available = bool(definition and address and community
                         and str(details.get("version", "2")) == "2")
        if available:
            groups.setdefault((address, port, community), []).append(definition)
        rows.append({"id": str(item["itemid"]), "label": item["name"],
                     "units": item.get("units") or "", "available": available})
    result = {"serial": serial.upper(), "rows": rows, "groups": groups}
    with _cache_lock:
        _olt_live_cache[cache_key] = (time.monotonic(), result)
    return result


def olt_latest_values(host_id, item_ids):
    """Read uncached Zabbix values for ONU items without direct SNMP access."""
    if not item_ids:
        return {}
    items = zabbix_call("item.get", {
        "output": ["itemid", "hostid", "status", "lastvalue", "lastclock"],
        "hostids": [host_id], "itemids": item_ids,
    })
    return {str(item["itemid"]): item.get("lastvalue")
            if int(item.get("lastclock") or 0) > 0 else None
            for item in items if str(item.get("hostid")) == str(host_id)
            and str(item.get("status")) == "0"
            and str(item.get("itemid")) in item_ids}


def clear_caches():
    global _olt_hosts_cache
    with _cache_lock:
        _cache.clear()
        _catalog_cache.clear()
        _modem_items_cache.clear()
        _modem_restarts_cache.clear()
        _modem_restarts_daily_cache.clear()
        _optical_items_cache.clear()
        _ping_loss_items_cache.clear()
        _olt_hosts_cache = None
        _olt_items_cache.clear()
        _olt_live_cache.clear()
