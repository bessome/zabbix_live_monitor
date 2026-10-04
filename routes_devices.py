"""Device lists, live monitors and modem history routes."""
import asyncio
import math
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app_storage import setting, snmp_community_for
from app_web import render, require_user
from ping_monitor import snapshot_for
from snmp_monitor import display_modem_values, snapshot_for as snmp_snapshot_for
from switch_monitor import (DEFAULT_EXCLUDED_NAMES, parse_excluded_names,
                            snapshot_for as switch_snapshot_for)
from zabbix_service import (category_filter, device_descriptions, host_rows,
                            modem_channel_definitions, normalize_device_search,
                            optical_power_definitions, ping_loss_definition,
                            zabbix_call)

router = APIRouter()
HISTORY_PERIODS = {"1h": 3600, "12h": 43200, "24h": 86400, "2d": 172800}
HISTORY_PAGE_SIZE = 50000


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


@router.get("/api/devices/{category}")
async def devices_api(request: Request, category: str, q: str = "",
                      page: int = 1, per_page: int = 50):
    require_user(request, api=True)
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
    devices_with_description = [
        {**row, "device_description": descriptions.get(row["id"])}
        for row in visible
    ]
    return {"devices": devices_with_description, "total": total,
            "page": page, "per_page": per_page, "pages": pages,
            "updated_at": int(time.time())}


@router.get("/devices/{category}/{host_id}")
async def device_detail(request: Request, category: str, host_id: str):
    require_user(request)
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
    return render(request, "device.html", category=category, device=device)


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
