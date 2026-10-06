"""Device lists, live monitors and modem history routes."""
import asyncio
import ipaddress
import math
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app_storage import (favorite_devices_for, favorite_ids_for,
                         recent_devices_for, record_recent_device, remove_favorite,
                         save_favorite, setting, snmp_community_for,
                         snmp_write_community_for)
from app_web import checked_form, render, require_user
from cable_test import CableTestBusy, run_cable_test
from ping_monitor import snapshot_for
from snmp_monitor import (display_modem_values, snapshot_for as snmp_snapshot_for,
                          uptime_for)
from switch_monitor import (DEFAULT_EXCLUDED_NAMES, parse_excluded_names,
                            snapshot_for as switch_snapshot_for)
from zabbix_service import (category_filter, device_descriptions, host_rows,
                            modem_channel_definitions, modem_restarts_count_24h,
                            modem_restarts_item, normalize_device_search,
                            optical_power_definitions, ping_loss_definition,
                            zabbix_call)

router = APIRouter()
HISTORY_PERIODS = {"1h": 3600, "12h": 43200, "24h": 86400,
                   "2d": 172800, "14d": 1209600}
HISTORY_PAGE_SIZE = 50000


def device_http_url(device):
    """Use only a literal interface IP when linking to a device's web UI."""
    for value in (device.get("interface_ip"), device.get("address"),
                  device.get("snmp_address")):
        try:
            ip = ipaddress.ip_address(value)
        except ValueError:
            continue
        return f"http://[{ip.compressed}]" if ip.version == 6 else f"http://{ip.compressed}"
    return None


def plot_points(points, max_points=1200):
    """Keep each time bucket's low and high value for a compact graph."""
    if len(points) <= max_points:
        return points
    bucket_count = (max_points - 2) // 2
    bucket_size = math.ceil((len(points) - 2) / bucket_count)
    reduced = [points[0]]
    for start in range(1, len(points) - 1, bucket_size):
        end = min(start + bucket_size, len(points) - 1)
        lowest = min(range(start, end), key=lambda index: points[index]["value"])
        highest = max(range(start, end), key=lambda index: points[index]["value"])
        reduced.extend(points[index] for index in sorted({lowest, highest}))
    reduced.append(points[-1])
    return reduced


async def numeric_history(item, period, now=None):
    value_type = int(item.get("value_type", 0))
    if value_type not in (0, 3):
        raise HTTPException(422, "Для этого item нет числовой истории.")
    now = int(time.time()) if now is None else now
    from_time = now - HISTORY_PERIODS[period]
    if period == "14d":
        trends = await asyncio.to_thread(zabbix_call, "trend.get", {
            "output": ["clock", "value_avg"], "itemids": [item["id"]],
            "time_from": from_time, "time_till": now, "limit": 500,
        })
        trend_points = []
        for row in trends:
            try:
                timestamp = int(row["clock"])
                value = float(row["value_avg"])
            except (KeyError, TypeError, ValueError):
                continue
            if math.isfinite(value):
                trend_points.append({"time": timestamp, "value": value})
        if trend_points:
            return {"label": item["label"], "units": item["units"],
                    "period": period, "from": from_time, "to": now,
                    "aggregation": "hourly_average",
                    "points": sorted(trend_points, key=lambda point: point["time"])}
    history = []
    cursor = now
    while cursor >= from_time:
        page = await asyncio.to_thread(zabbix_call, "history.get", {
            "output": ["clock", "value"], "itemids": [item["id"]],
            "history": value_type, "time_from": from_time, "time_till": cursor,
            "sortfield": "clock", "sortorder": "DESC", "limit": HISTORY_PAGE_SIZE,
        })
        history.extend(page)
        if len(page) < HISTORY_PAGE_SIZE:
            break
        cursor = int(page[-1]["clock"]) - 1
    points = []
    for row in reversed(history):
        try:
            timestamp = int(row["clock"])
            value = float(row["value"])
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(value):
            points.append({"time": timestamp, "value": value})
    return {"label": item["label"], "units": item["units"],
            "period": period, "from": from_time, "to": now,
            "points": plot_points(points)}

@router.get("/devices/{category}")
def devices(request: Request, category: str):
    require_user(request)
    mode, ids = category_filter(category)
    return render(request, "devices.html", category=category,
                   mode=mode, configured=bool(ids))


@router.get("/favorites")
def favorites_page(request: Request):
    user = require_user(request)
    favorites = favorite_devices_for(user["id"])
    recent = recent_devices_for(user["id"])
    favorite_ids = {device["host_id"] for device in favorites}
    return render(request, "favorites.html", favorites=favorites, recent=recent,
                  favorite_ids=favorite_ids)


@router.post("/api/favorites/{category}/{host_id}")
async def set_favorite(request: Request, category: str, host_id: str):
    user = require_user(request, api=True)
    category_filter(category)
    form = await checked_form(request)
    action = str(form.get("action", ""))
    if action == "remove":
        remove_favorite(user["id"], host_id)
        return JSONResponse({"favorite": False}, headers={"Cache-Control": "no-store"})
    if action != "add":
        raise HTTPException(400, "Invalid favorite action")
    try:
        devices = await asyncio.to_thread(host_rows, category)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    device = next((row for row in devices if row["id"] == host_id), None)
    if device is None:
        raise HTTPException(404)
    save_favorite(user["id"], device, category)
    return JSONResponse({"favorite": True}, headers={"Cache-Control": "no-store"})


@router.get("/api/devices/{category}")
async def devices_api(request: Request, category: str, q: str = "",
                       page: int = 1, per_page: int = 50):
    user = require_user(request, api=True)
    category_filter(category)
    if page < 1 or per_page not in (25, 50, 100, 250):
        raise HTTPException(422, "Invalid pagination parameters")
    try:
        rows = await asyncio.to_thread(host_rows, category)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    query = normalize_device_search(q.strip()[:100])
    if query:
        rows = [row for row in rows if any(
            query in normalize_device_search(str(row[field]))
            for field in ("name", "technical_name", "address"))]
    total = len(rows)
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, pages)
    start = (page - 1) * per_page
    visible = rows[start:start + per_page]
    try:
        descriptions = await asyncio.to_thread(
            device_descriptions, [row["id"] for row in visible])
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    favorite_ids = favorite_ids_for(user["id"])
    devices_with_description = [
        {**row, "device_description": descriptions.get(row["id"]),
         "favorite": row["id"] in favorite_ids}
        for row in visible
    ]
    return {"devices": devices_with_description, "total": total,
            "page": page, "per_page": per_page, "pages": pages,
            "updated_at": int(time.time())}


@router.get("/devices/{category}/{host_id}")
async def device_detail(request: Request, category: str, host_id: str):
    user = require_user(request)
    category_filter(category)
    try:
        rows = await asyncio.to_thread(host_rows, category)
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    device = next((row for row in rows if row["id"] == host_id), None)
    if device is None:
        raise HTTPException(404)
    try:
        descriptions = await asyncio.to_thread(device_descriptions, [host_id])
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    device = {**device, "device_description": descriptions.get(host_id)}
    record_recent_device(user["id"], device, category)
    return render(request, "device.html", category=category, device=device,
                  favorite=host_id in favorite_ids_for(user["id"]),
                  device_http_url=device_http_url(device))


@router.get("/api/devices/{category}/{host_id}/ping")
async def device_ping(request: Request, category: str, host_id: str):
    require_user(request, api=True)
    category_filter(category)
    try:
        rows = await asyncio.to_thread(host_rows, category)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    device = next((row for row in rows if row["id"] == host_id), None)
    if device is None:
        raise HTTPException(404)
    try:
        snapshot = snapshot_for(host_id, device["address"])
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    return JSONResponse(snapshot, headers={"Cache-Control": "no-store"})


@router.get("/api/devices/{category}/{host_id}/uptime")
async def device_uptime(request: Request, category: str, host_id: str):
    require_user(request, api=True)
    category_filter(category)
    try:
        rows = await asyncio.to_thread(host_rows, category)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    device = next((row for row in rows if row["id"] == host_id), None)
    if device is None:
        raise HTTPException(404)
    try:
        seconds = await uptime_for(host_id, device["snmp_address"],
                                   device["snmp_port"], snmp_community_for(category))
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    return JSONResponse({"seconds": seconds}, headers={"Cache-Control": "no-store"})


@router.get("/api/devices/{category}/{host_id}/modem-channels")
async def modem_channels(request: Request, category: str, host_id: str):
    require_user(request, api=True)
    if category != "Modems":
        raise HTTPException(404)
    try:
        rows = await asyncio.to_thread(host_rows, category)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    device = next((row for row in rows if row["id"] == host_id), None)
    if device is None:
        raise HTTPException(404)
    definitions = []
    try:
        definitions = await asyncio.to_thread(modem_channel_definitions, host_id)
        community = snmp_community_for(category)
        result = await snmp_snapshot_for(host_id, device["snmp_address"],
                                         device["snmp_port"], community, definitions)
    except (RuntimeError, ValueError) as exc:
        return JSONResponse({"error": str(exc), "items": display_modem_values(definitions, [])},
                            status_code=503)
    return JSONResponse({**result, "items": display_modem_values(definitions, result["items"])},
                        headers={"Cache-Control": "no-store"})


@router.get("/api/devices/{category}/{host_id}/modem-restarts")
async def modem_restarts(request: Request, category: str, host_id: str):
    require_user(request, api=True)
    if category != "Modems":
        raise HTTPException(404)
    try:
        rows = await asyncio.to_thread(host_rows, category)
        if not any(row["id"] == host_id for row in rows):
            raise HTTPException(404)
        item = await asyncio.to_thread(modem_restarts_item, host_id)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    count_24h = None
    if item is not None:
        try:
            count_24h = await asyncio.to_thread(modem_restarts_count_24h,
                                                host_id, item)
        except RuntimeError:
            pass
    return JSONResponse({"item": item, "count_24h": count_24h},
                        headers={"Cache-Control": "no-store"})


@router.get("/api/devices/{category}/{host_id}/switch-ports")
async def switch_ports(request: Request, category: str, host_id: str):
    require_user(request, api=True)
    if category != "Switches":
        raise HTTPException(404)
    try:
        rows = await asyncio.to_thread(host_rows, category)
        device = next((row for row in rows if row["id"] == host_id), None)
        if device is None:
            raise HTTPException(404)
        community = snmp_community_for(category)
        excluded_names = parse_excluded_names(
            setting("switch_port_exclude", DEFAULT_EXCLUDED_NAMES))
        result = await switch_snapshot_for(host_id, device["snmp_address"],
                                           device["snmp_port"], community,
                                           excluded_names)
    except (RuntimeError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.post("/api/devices/{category}/{host_id}/switch-ports/{if_index}/cable-test")
async def switch_cable_test(request: Request, category: str, host_id: str, if_index: int):
    user = require_user(request, api=True)
    if category != "Switches":
        raise HTTPException(404)
    if user["role"] not in ("execute", "admin"):
        raise HTTPException(403)
    await checked_form(request)
    try:
        rows = await asyncio.to_thread(host_rows, category)
        device = next((row for row in rows if row["id"] == host_id), None)
        if device is None:
            raise HTTPException(404)
        read_community = snmp_community_for(category)
        write_community = snmp_write_community_for(category)
        excluded_names = parse_excluded_names(
            setting("switch_port_exclude", DEFAULT_EXCLUDED_NAMES))
        snapshot = await switch_snapshot_for(host_id, device["snmp_address"],
                                             device["snmp_port"], read_community,
                                             excluded_names)
        selected = next((port for port in snapshot["ports"]
                         if port["index"] == if_index), None)
        if selected is None:
            raise HTTPException(404)
        result = await run_cable_test(host_id, device["snmp_address"],
                                      device["snmp_port"], read_community,
                                      write_community, selected["name"])
    except CableTestBusy:
        return JSONResponse({"error": "На этом коммутаторе уже идёт тест кабеля."},
                            status_code=409)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except (RuntimeError, OSError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.get("/api/devices/{category}/{host_id}/optical-power")
async def optical_power(request: Request, category: str, host_id: str):
    require_user(request, api=True)
    if category != "TV_Amplifires":
        raise HTTPException(404)
    definitions = []
    try:
        rows = await asyncio.to_thread(host_rows, category)
        device = next((row for row in rows if row["id"] == host_id), None)
        if device is None:
            raise HTTPException(404)
        definitions = await asyncio.to_thread(optical_power_definitions, host_id)
        community = snmp_community_for(category)
        result = await snmp_snapshot_for(host_id, device["snmp_address"],
                                         device["snmp_port"], community, definitions)
    except (RuntimeError, ValueError) as exc:
        return JSONResponse({"error": str(exc), "items": [
            {"id": item["id"], "label": item["label"], "value": None,
             "units": item["units"]} for item in definitions
        ]}, status_code=503)
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.get("/api/devices/{category}/{host_id}/modem-channels/{item_id}/history")
async def modem_channel_history(request: Request, category: str, host_id: str,
                                item_id: str, period: str = "1h"):
    require_user(request, api=True)
    if category != "Modems":
        raise HTTPException(404)
    if period not in HISTORY_PERIODS:
        raise HTTPException(422, "Неверный период графика.")
    try:
        rows = await asyncio.to_thread(host_rows, category)
        if not any(row["id"] == host_id for row in rows):
            raise HTTPException(404)
        definitions = await asyncio.to_thread(modem_channel_definitions, host_id)
        item = next((entry for entry in definitions if entry["id"] == item_id), None)
        if item is None:
            raise HTTPException(404)
        error_item = next((entry for entry in definitions
                           if entry["id"] == item.get("error_rate_id")), None)
        if error_item is None:
            result = await numeric_history(item, period)
        else:
            now = int(time.time())
            result, error_history = await asyncio.gather(
                numeric_history(item, period, now), numeric_history(error_item, period, now))
            result["secondary"] = {"label": "Ошибки/с", "units": "ош/с",
                                   "points": error_history["points"]}
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.get("/api/devices/{category}/{host_id}/modem-restarts/{item_id}/history")
async def modem_restarts_history(request: Request, category: str, host_id: str,
                                 item_id: str, period: str = "1h"):
    require_user(request, api=True)
    if category != "Modems":
        raise HTTPException(404)
    if period not in HISTORY_PERIODS:
        raise HTTPException(422, "Неверный период графика.")
    try:
        rows = await asyncio.to_thread(host_rows, category)
        if not any(row["id"] == host_id for row in rows):
            raise HTTPException(404)
        item = await asyncio.to_thread(modem_restarts_item, host_id)
        if item is None or item["id"] != item_id:
            raise HTTPException(404)
        result = await numeric_history(item, period)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.get("/api/devices/{category}/{host_id}/ping-loss/history")
async def ping_loss_history(request: Request, category: str, host_id: str,
                            period: str = "1h"):
    require_user(request, api=True)
    category_filter(category)
    if period not in HISTORY_PERIODS:
        raise HTTPException(422, "Неверный период графика.")
    try:
        rows = await asyncio.to_thread(host_rows, category)
        if not any(row["id"] == host_id for row in rows):
            raise HTTPException(404)
        item = await asyncio.to_thread(ping_loss_definition, host_id)
        if item is None:
            return JSONResponse({"error": "В Zabbix нет включённого item icmppingloss для этого устройства."},
                                status_code=404)
        result = await numeric_history(item, period)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.get("/api/devices/{category}/{host_id}/optical-power/{item_id}/history")
async def optical_power_history(request: Request, category: str, host_id: str,
                                item_id: str, period: str = "1h"):
    require_user(request, api=True)
    if category != "TV_Amplifires":
        raise HTTPException(404)
    if period not in HISTORY_PERIODS:
        raise HTTPException(422, "Неверный период графика.")
    try:
        rows = await asyncio.to_thread(host_rows, category)
        if not any(row["id"] == host_id for row in rows):
            raise HTTPException(404)
        definitions = await asyncio.to_thread(optical_power_definitions, host_id)
        item = next((entry for entry in definitions if entry["id"] == item_id), None)
        if item is None:
            raise HTTPException(404)
        result = await numeric_history(item, period)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    return JSONResponse(result, headers={"Cache-Control": "no-store"})
