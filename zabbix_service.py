"""Zabbix API access, device mapping and short-lived caches."""
import json
import math
import os
import re
import threading
import time
from urllib import error, request as urlrequest

from fastapi import HTTPException

from app_config import CATEGORIES
from app_storage import setting
from snmp_monitor import channel_definition, link_error_rates, optical_definition

_cache = {}
_catalog_cache = {}
_modem_items_cache = {}
_modem_restarts_cache = {}
_modem_restarts_daily_cache = {}
_optical_items_cache = {}
_ping_loss_items_cache = {}
_cache_lock = threading.Lock()
_search_letters = str.maketrans({"ä": "a", "ö": "o", "õ": "o", "ü": "u"})

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


def zabbix_call(method, params):
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
        with urlrequest.urlopen(api_request, timeout=8) as response:
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
                      "enabled": host["status"] == "0", "availability": availability})
    with _cache_lock:
        _cache[category] = (time.monotonic(), rows)
    return rows


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


def clear_caches():
    with _cache_lock:
        _cache.clear()
        _catalog_cache.clear()
        _modem_items_cache.clear()
        _modem_restarts_cache.clear()
        _modem_restarts_daily_cache.clear()
        _optical_items_cache.clear()
        _ping_loss_items_cache.clear()
