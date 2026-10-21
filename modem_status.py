"""Current Cisco MAC state, including wideband and downstream partial service."""
import asyncio
import hashlib
import time

from pysnmp.hlapi.v3arch.asyncio import SnmpEngine
from pysnmp.proto.rfc1905 import EndOfMibView, NoSuchInstance, NoSuchObject

from cmts_groups import _macs, _walk
from cmts_cli import configuration as cli_configuration, read_status as cli_status
from cmts_monitor import (IP_COLUMN, MAC_COLUMN, MAC_POINTER, _address, _get,
                          _target, mac_instance, normalize_mac)

CISCO_STATE = "1.3.6.1.4.1.9.9.116.1.3.2.1.1"
DOCSIS_STATE = "1.3.6.1.2.1.10.127.1.3.3.1.9"
WIDEBAND_STATE = "1.3.6.1.4.1.9.9.479.1.6.1.1"
RESIL_BASE = "1.3.6.1.4.1.9.9.116.1.5.13.1"
INVALID = (EndOfMibView, NoSuchInstance, NoSuchObject)
CACHE_SECONDS = 4.5
_entries = {}
_catalogs = {}
_limit = asyncio.Semaphore(4)

# CLI labels from CISCO-DOCS-EXT-MIB. Preserve security and rejection details.
STATES = {
    1: "offline", 3: "init(r1)", 4: "init(d)", 5: "online(d)",
    6: "online(pk)", 7: "online(pt)", 8: "reject(m)", 9: "reject(c)",
    10: "reject(pk)", 11: "reject(pt)", 12: "online", 13: "init(o)",
    14: "init(t)", 15: "reset", 16: "init(r2)", 17: "init(rc)",
    18: "init(i)", 19: "reject(ts)", 20: "reject(ip)",
    21: "reject(c)", 22: "reject(na)", 23: "expire(pk)",
    24: "expire(pt)", 25: "shutdown", 26: "cc(r1)", 27: "cc(r2)",
}
WB_STATES = {
    27: "w-online", 28: "w-online(p)", 29: "w-online(pk)",
    30: "w-online(pt)", 31: "w-online(d)", 32: "w-reject(pk)",
    33: "w-reject(pt)", 34: "w-reject(d)", 35: "w-reject(p)",
    36: "w-expire(pk)", 37: "w-expire(pt)", 38: "w-expire(d)",
    39: "w-expire(p)",
}


def _integer(value):
    if isinstance(value, INVALID):
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def _mac(value):
    if isinstance(value, INVALID):
        return None
    try:
        compact = bytes(value.asOctets()).hex()
    except (ValueError, AttributeError, TypeError):
        return None
    return compact if len(compact) == 12 else None


def decode_state(cisco, docsis, wideband, partial=None):
    """Never infer full service when wideband partial-service data is unknown."""
    no_wideband_row = isinstance(wideband, NoSuchInstance)
    cisco, docsis, wideband = map(_integer, (cisco, docsis, wideband))
    if cisco == 1:
        return "offline"
    if cisco in (3, 4, 13, 14, 15, 16, 17, 18, 25, 26, 27):
        return STATES[cisco]
    if wideband in WB_STATES:
        label = WB_STATES[wideband]
        if partial is None:
            return None
        return label.replace("w-", "p-", 1) if partial else label
    if cisco in STATES and cisco not in (5, 6, 7, 12):
        return STATES[cisco]
    if no_wideband_row and cisco in (5, 6, 7, 12):
        return STATES.get(cisco)
    # Without the WB object an online modem's registration mode is unknown.
    if wideband == 12 or wideband in (5, 6, 7):
        return STATES.get(cisco) if cisco in (5, 6, 7, 12) else STATES.get(wideband)
    if cisco in (None, 2):
        return {2: "init(ranging)", 3: "init(ranging-aborted)",
                4: "init(ranging-complete)", 5: "init(ip-complete)",
                7: "reject", 8: "init", 9: "init(bpi)", 10: "offline"}.get(docsis)
    return None


class _Entry:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.last_seen = time.monotonic()
        self.updated = 0
        self.data = None
        self.location = None
        self.unsupported = False


def _entry(cache, key):
    now = time.monotonic()
    for old_key, entry in list(cache.items()):
        if now - entry.last_seen > 120:
            del cache[old_key]
    if key not in cache:
        if len(cache) >= 160:
            raise RuntimeError("Слишком много активных опросов статуса.")
        cache[key] = _Entry()
    cache[key].last_seen = now
    return cache[key]


def _config_key(cmts):
    return (cmts["id"], cmts["address"], cmts["port"],
            hashlib.sha256(cmts["community"].encode()).digest())


async def _catalog(cmts, partial=False):
    """Cache IP identities for a minute and share fresh resiliency MACs across viewers."""
    entry = _entry(_catalogs, (_config_key(cmts), partial))
    async with entry.lock:
        unsupported_error = ("CMTS не предоставляет " + RESIL_BASE
                             + ".2 через SNMP (No Such Object); "
                             "нельзя различить w-online и p-online.")
        if entry.unsupported and time.monotonic() - entry.updated < 60:
            raise RuntimeError(unsupported_error)
        ttl = CACHE_SECONDS if partial else 60
        if entry.data is not None and time.monotonic() - entry.updated < ttl:
            return entry.data
        started = time.monotonic()
        async with _limit:
            target = await _target(cmts)
            with SnmpEngine() as engine:
                bases = [RESIL_BASE + ".2"] if partial else [MAC_COLUMN, IP_COLUMN]
                if partial:
                    # A GET of the column without an index distinguishes a missing
                    # object from a supported table with no current instances.
                    probe = (await _get(engine, target, cmts["community"], bases))[0]
                    if isinstance(probe, NoSuchObject):
                        entry.data = None
                        entry.unsupported = True
                        entry.updated = started
                        raise RuntimeError(unsupported_error)
                    if not isinstance(probe, NoSuchInstance):
                        raise RuntimeError("CMTS не подтвердила поддержку таблицы partial service.")
                    entry.unsupported = False
                columns = await asyncio.wait_for(asyncio.gather(*(
                    _walk(engine, target, cmts["community"], base) for base in bases
                )), 35)
        # An unsupported column must not be interpreted as an empty valid table.
        if any(isinstance(value, (NoSuchObject, NoSuchInstance))
               for column in columns for value in column.values()):
            raise RuntimeError("CMTS не поддерживает таблицу partial service.")
        entry.data = columns
        entry.updated = started
        return columns


async def _locate_one(cmts, mac, addresses):
    if mac:
        async with _limit:
            target = await _target(cmts)
            with SnmpEngine() as engine:
                index = _integer((await _get(engine, target, cmts["community"],
                    [MAC_POINTER + "." + mac_instance(mac)]))[0])
                if index is not None and index > 0:
                    actual = (await _get(engine, target, cmts["community"],
                                        [MAC_COLUMN + "." + str(index)]))[0]
                    if _mac(actual) == mac:
                        return cmts, str(index), mac
        # Some IOS versions retain offline rows but no longer expose their pointer.
    macs, ips = await _catalog(cmts)
    matches = [(cmts, index, _mac(value)) for index, value in macs.items()
               if _mac(value) and (_mac(value) == mac if mac else
                                  _address(ips.get(index, NoSuchInstance())) in addresses)]
    if len(matches) > 1:
        raise RuntimeError("Несколько модемов с одним IP на CMTS.")
    return matches[0] if matches else None


async def _partial_service(cmts, engine, target, mac):
    columns = await _catalog(cmts, partial=True)
    indices = [index for index, value in columns[0].items() if _mac(value) == mac]
    if not indices:
        # Some IOS versions include only modems in partial service in this table.
        return False
    if len(indices) != 1:
        raise RuntimeError("Неоднозначный индекс partial service.")
    suffix = "." + indices[0]
    values = await _get(engine, target, cmts["community"],
                       [RESIL_BASE + "." + str(column) + suffix
                        for column in (2, 3, 5, 7)])
    if _mac(values[0]) != mac:
        _catalogs.pop((_config_key(cmts), True), None)
        raise RuntimeError("Индекс partial service изменился; повторный опрос.")
    total, active = map(_integer, values[1:3])
    impaired = values[3]
    if total is None or active is None or isinstance(impaired, INVALID):
        raise RuntimeError("CMTS не возвращает данные partial service.")
    if total <= 0 or active < 0 or active > total:
        raise RuntimeError("CMTS вернула некорректное число DS-каналов.")
    return active < total or bool(impaired.prettyPrint().strip())


async def _read(location):
    cmts, index, mac = location
    target = await _target(cmts)
    with SnmpEngine() as engine:
        values = await _get(engine, target, cmts["community"],
                           [base + "." + index for base in
                            (MAC_COLUMN, CISCO_STATE, DOCSIS_STATE, WIDEBAND_STATE)])
        if _mac(values[0]) != mac:
            raise LookupError("Индекс модема изменился.")
        partial = None
        error = None
        if _integer(values[3]) in WB_STATES and decode_state(*values[1:]) is None:
            try:
                partial = await _partial_service(cmts, engine, target, mac)
            except (RuntimeError, OSError, ValueError, asyncio.TimeoutError) as exc:
                error = str(exc) or "Таймаут partial service."
        label = decode_state(*values[1:], partial)
    return {"status": label, "cmts": cmts["name"], "mac": mac,
            "error": (error or "CMTS не возвращает достаточно данных для точного статуса.")
                     if label is None else None,
            "updated_at": int(time.time())}


async def status_for(configs, device=None, mac=None):
    """Read fresh status independently of modem reachability and Zabbix history."""
    device = device or {}
    if mac:
        mac = normalize_mac(mac)
        if len(mac) != 12:
            raise ValueError("Нужен полный MAC модема.")
    else:
        candidates = set().union(*(_macs(device.get(field))
                                   for field in ("name", "technical_name", "mac")))
        if len(candidates) > 1:
            raise RuntimeError("В данных модема несколько MAC-адресов.")
        mac = next(iter(candidates), None)
    addresses = {device.get(field) for field in
                 ("interface_ip", "snmp_address", "address")} - {None, "", "—"}
    use_cli, cli_user = cli_configuration()
    key = (tuple(_config_key(cmts) for cmts in configs), mac, tuple(sorted(addresses)),
           use_cli, cli_user)
    entry = _entry(_entries, key)
    async with entry.lock:
        if entry.data and time.monotonic() - entry.updated < CACHE_SECONDS:
            return entry.data
        started = time.monotonic()
        try:
            if not configs:
                raise RuntimeError("Добавьте CMTS в настройках.")
            if not mac and not addresses:
                raise RuntimeError("Нет MAC или IP для поиска модема на CMTS.")
            if entry.location is None:
                outcomes = await asyncio.wait_for(asyncio.gather(*(
                    _locate_one(cmts, mac, addresses) for cmts in configs
                ), return_exceptions=True), 35)
                matches = [value for value in outcomes if value is not None
                           and not isinstance(value, BaseException)]
                if len(matches) != 1:
                    raise RuntimeError("Модем не найден однозначно на CMTS.")
                if any(isinstance(value, BaseException) for value in outcomes):
                    raise RuntimeError("Не все CMTS доступны; принадлежность модема не подтверждена.")
                entry.location = matches[0]
            try:
                if use_cli:
                    cmts, _, compact = entry.location
                    label = await cli_status(cmts, compact, cli_user)
                    entry.data = {"status": label, "cmts": cmts["name"], "mac": compact,
                                  "source": "cli", "error": None,
                                  "updated_at": int(time.time())}
                else:
                    entry.data = await _read(entry.location)
            except LookupError:
                _catalogs.pop((_config_key(entry.location[0]), False), None)
                entry.location = None
                raise RuntimeError("Индекс модема изменился; выполняется повторный поиск.")
        except (RuntimeError, OSError, ValueError, asyncio.TimeoutError) as exc:
            entry.data = {"status": None, "error": str(exc) or "Таймаут CMTS.",
                          "updated_at": int(time.time())}
        if entry.data.get("status") is None:
            targets = [entry.location[0]] if entry.location else configs
            addresses_text = ", ".join(str(cmts["address"]) for cmts in targets)
            if addresses_text:
                entry.data["error"] = ("CMTS " + addresses_text + ": "
                                       + (entry.data.get("error") or "Статус недоступен."))
                entry.data["cmts_ip"] = entry.location[0]["address"] if entry.location else None
        entry.updated = started
        return entry.data
