"""Preview and synchronize Zabbix modem groups from CMTS SNMP tables."""
import asyncio
import hashlib
import json
import re

from pysnmp.hlapi.v3arch.asyncio import (
    CommunityData, ContextData, ObjectIdentity, ObjectType, SnmpEngine,
    bulk_walk_cmd,
)

from cmts_monitor import IP_COLUMN, MAC_COLUMN, _address, _target

IF_ALIAS = "1.3.6.1.2.1.31.1.1.1.18"
UPSTREAM = "1.3.6.1.2.1.10.127.1.3.3.1.5"
DOCSIS3_UPSTREAM = "1.3.6.1.4.1.4491.2.1.20.1.4.1.2"
MAX_ROWS = 100000
SCAN_SECONDS = 120
_MAC = re.compile(r"(?<![0-9a-fA-F])(?:[0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}(?![0-9a-fA-F])|(?<![0-9a-fA-F])[0-9a-fA-F]{4}(?:\.[0-9a-fA-F]{4}){2}(?![0-9a-fA-F])|(?<![0-9a-fA-F])[0-9a-fA-F]{12}(?![0-9a-fA-F])")


def _macs(text):
    return {re.sub(r"[^0-9a-fA-F]", "", match.group()).lower()
            for match in _MAC.finditer(str(text or ""))}


def group_segment(alias):
    """Keep the description readable while avoiding an extra Zabbix group level."""
    return " ".join(alias.strip().replace("/", " - ").split())


async def _walk(engine, target, community, base):
    rows = {}
    async for indication, status, _, bindings in bulk_walk_cmd(
        engine, CommunityData(community, mpModel=1), target, ContextData(),
        0, 25, ObjectType(ObjectIdentity(base)), lexicographicMode=False,
        lookupMib=False, maxRows=MAX_ROWS + 1,
    ):
        if indication:
            raise RuntimeError("SNMP: " + str(indication))
        if status:
            raise RuntimeError("SNMP: " + status.prettyPrint())
        for oid, value in bindings:
            name = oid.prettyPrint()
            if name.startswith(base + "."):
                rows[name[len(base) + 1:]] = value
                if len(rows) > MAX_ROWS:
                    raise RuntimeError("SNMP-таблица слишком велика; проверка остановлена.")
    return rows


async def scan(cmts):
    """Take one bounded SNMP snapshot; all columns are joined by CM index."""
    async def read():
        target = await _target(cmts)
        with SnmpEngine() as engine:
            aliases, macs, ips, old_up, new_up = await asyncio.gather(*(
                _walk(engine, target, cmts["community"], base)
                for base in (IF_ALIAS, MAC_COLUMN, IP_COLUMN, UPSTREAM, DOCSIS3_UPSTREAM)
            ))
        return aliases, macs, ips, old_up, new_up

    try:
        aliases, macs, ips, old_up, new_up = await asyncio.wait_for(
            read(), SCAN_SECONDS)
    except asyncio.TimeoutError as exc:
        raise RuntimeError("Превышено время опроса CMTS.") from exc
    alias_by_index = {}
    for index, value in aliases.items():
        try:
            alias_by_index[int(index)] = value.asOctets().decode("utf-8", "replace").strip()
        except (ValueError, AttributeError):
            alias_by_index[int(index)] = value.prettyPrint().strip()
    channels = {}
    for instance in new_up:
        parts = instance.split(".")
        if len(parts) == 2 and all(part.isdecimal() for part in parts):
            channels.setdefault(parts[0], set()).add(int(parts[1]))
    for instance, value in old_up.items():
        if instance not in channels:
            try:
                channels[instance] = {int(value)}
            except (ValueError, TypeError):
                pass
    modems = []
    for index, value in macs.items():
        try:
            mac = bytes(value.asOctets()).hex()
        except (AttributeError, ValueError):
            continue
        if len(mac) != 12:
            continue
        ifindices = channels.get(index, set())
        labels = {alias_by_index.get(ifindex, "") for ifindex in ifindices}
        alias = next(iter(labels)) if len(labels) == 1 and "" not in labels else ""
        modems.append({"index": index, "mac": mac, "ip": _address(ips[index])
                       if index in ips else None, "alias": alias,
                       "ifindices": sorted(ifindices),
                       "reason": "" if alias else
                       ("нет upstream" if not ifindices else "нет единого описания upstream")})
    return {"modems": modems}


def _hosts_for_modems():
    from zabbix_service import category_filter, zabbix_call

    mode, ids = category_filter("Modems")
    if not ids:
        raise RuntimeError("Сначала задайте фильтр категории Modems в настройках Zabbix.")
    return zabbix_call("host.get", {
        "output": ["hostid", "host", "name"],
        "selectGroups": ["groupid", "name"],
        "selectInterfaces": ["ip", "dns"],
        "selectInventory": ["macaddress_a", "macaddress_b"],
        "groupids" if mode == "group" else "templateids": ids,
    })


def make_plan(city, snapshot, hosts, groups):
    prefix = city + "/modems"
    existing = {group["name"]: str(group["groupid"]) for group in groups}
    host_by_mac = {}
    host_by_ip = {}
    host_macs = {}
    for host in hosts:
        inventory = host.get("inventory") or {}
        macs = set().union(*(_macs(value) for value in (
            host.get("host"), host.get("name"),
            inventory.get("macaddress_a"), inventory.get("macaddress_b"))))
        host_macs[str(host["hostid"])] = macs
        for mac in macs:
            host_by_mac.setdefault(mac, []).append(host)
        for interface in host.get("interfaces", []):
            if interface.get("ip"):
                host_by_ip.setdefault(interface["ip"], []).append(host)
    assignments = []
    skipped = []
    seen_hosts = set()
    occupied_groups = set()
    modem_ip_counts = {}
    for modem in snapshot["modems"]:
        if modem["ip"]:
            modem_ip_counts[modem["ip"]] = modem_ip_counts.get(modem["ip"], 0) + 1
    for modem in snapshot["modems"]:
        if not modem["alias"]:
            skipped.append({**modem, "reason": modem["reason"]})
            continue
        matches = host_by_mac.get(modem["mac"], [])
        match_method = "MAC"
        if not matches and modem["ip"] and modem_ip_counts[modem["ip"]] == 1:
            matches = [host for host in host_by_ip.get(modem["ip"], [])
                       if not host_macs[str(host["hostid"])]]
            match_method = "IP"
        matches = {str(host["hostid"]): host for host in matches}
        if len(matches) != 1:
            skipped.append({**modem, "reason": "хост Zabbix не найден" if not matches
                            else "несколько хостов Zabbix"})
            continue
        host = next(iter(matches.values()))
        hostid = str(host["hostid"])
        if hostid in seen_hosts:
            skipped.append({**modem, "reason": "хост совпал с несколькими модемами"})
            continue
        seen_hosts.add(hostid)
        group_name = prefix + "/" + group_segment(modem["alias"])
        occupied_groups.add(group_name)
        old_groups = host.get("groups", [])
        current = {str(group["groupid"]): group["name"] for group in old_groups}
        managed = {groupid for groupid, name in current.items()
                   if name.startswith(prefix + "/")}
        desired_id = existing.get(group_name)
        if desired_id in managed and len(managed) == 1:
            continue
        assignments.append({"hostid": hostid, "host": host["name"],
                            "mac": modem["mac"], "method": match_method,
                            "alias": modem["alias"], "group": group_name,
                            "has_target": desired_id in managed,
                            "remove_groupids": sorted(managed - {desired_id}),
                            "from": sorted(current[groupid] for groupid in managed)})
    new_groups = sorted((name for name in occupied_groups if name not in existing),
                        key=str.casefold)
    plan = {"city": city, "groups_to_create": new_groups,
            "assignments": assignments, "skipped": skipped,
            "modem_count": len(snapshot["modems"]),
            "area_count": len(occupied_groups)}
    plan["digest"] = hashlib.sha256(json.dumps(plan, sort_keys=True,
                                                ensure_ascii=False).encode()).hexdigest()
    return plan


async def build_plan(cmts):
    from zabbix_service import zabbix_call

    if not cmts["city"]:
        raise RuntimeError("Сначала укажите город в настройках CMTS.")
    snapshot = await scan(cmts)
    prefix = cmts["city"] + "/modems"
    hosts = await asyncio.to_thread(_hosts_for_modems)
    groups = await asyncio.to_thread(zabbix_call, "hostgroup.get", {
        "output": ["groupid", "name"], "search": {"name": prefix},
    })
    return make_plan(cmts["city"], snapshot, hosts, groups)


def apply_plan(plan):
    from zabbix_service import zabbix_call

    groups = zabbix_call("hostgroup.get", {"output": ["groupid", "name"],
                                            "search": {"name": plan["city"] + "/modems"}})
    ids = {group["name"]: str(group["groupid"]) for group in groups}
    created = 0
    changed = 0
    try:
        for name in plan["groups_to_create"]:
            if name not in ids:
                result = zabbix_call("hostgroup.create", {"name": name})
                ids[name] = str(result["groupids"][0])
                created += 1
        for assignment in plan["assignments"]:
            if not assignment["has_target"]:
                zabbix_call("hostgroup.massadd", {
                    "groups": [{"groupid": ids[assignment["group"]]}],
                    "hosts": [{"hostid": assignment["hostid"]}],
                })
            if assignment["remove_groupids"]:
                zabbix_call("hostgroup.massremove", {
                    "groupids": assignment["remove_groupids"],
                    "hostids": [assignment["hostid"]],
                })
            changed += 1
    except RuntimeError as exc:
        raise RuntimeError(f"Синхронизация прервана: создано групп {created}, "
                           f"обновлено модемов {changed}. {exc}") from exc
    return created, changed
