"""ONU/ONT item search through the OLT hosts in Zabbix group 100."""
import asyncio

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app_web import render, require_user
from routes_devices import HISTORY_PERIODS, numeric_history
from zabbix_service import olt_hosts, olt_items, zabbix_call

router = APIRouter()


@router.get("/onu-ont")
def onu_page(request: Request):
    require_user(request)
    return render(request, "onu.html")


@router.get("/api/onu-ont/olts")
async def onu_olts(request: Request):
    require_user(request, api=True)
    try:
        hosts = await asyncio.to_thread(olt_hosts)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    return JSONResponse({"olts": hosts}, headers={"Cache-Control": "no-store"})


@router.get("/api/onu-ont/items")
async def onu_items(request: Request, q: str = "", host_id: str = ""):
    require_user(request, api=True)
    query = q.strip()
    if len(query) > 120 or len(host_id) > 30:
        raise HTTPException(422, "Слишком длинный поисковый запрос.")
    try:
        hosts = await asyncio.to_thread(olt_hosts)
        visible = {host["id"]: host for host in hosts}
        if host_id and host_id not in visible:
            raise HTTPException(404)
        if not host_id and len(query) < 2:
            return JSONResponse({"items": [], "has_more": False,
                                 "hint": "Введите не менее двух символов или выберите OLT."},
                                headers={"Cache-Control": "no-store"})
        host_ids = [host_id] if host_id else list(visible)
        items, has_more = await asyncio.to_thread(olt_items, host_ids, query)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    result = []
    for item in items:
        host = visible.get(str(item.get("hostid")))
        if not host or (host_id and host["id"] != host_id):
            continue
        updated_at = int(item.get("lastclock") or 0)
        result.append({
            "id": str(item["itemid"]), "host_id": host["id"],
            "olt": host["name"], "name": item["name"],
            "key": item.get("key_") or "", "units": item.get("units") or "",
            "value": item.get("lastvalue") if updated_at else None,
            "updated_at": updated_at,
            "numeric": str(item.get("value_type")) in ("0", "3"),
        })
    return JSONResponse({"items": result, "has_more": has_more},
                        headers={"Cache-Control": "no-store"})


@router.get("/api/onu-ont/olts/{host_id}/items/{item_id}/history")
async def onu_item_history(request: Request, host_id: str, item_id: str,
                           period: str = "1h"):
    require_user(request, api=True)
    if period not in HISTORY_PERIODS:
        raise HTTPException(422, "Неверный период графика.")
    try:
        hosts = await asyncio.to_thread(olt_hosts)
        if not any(host["id"] == host_id for host in hosts):
            raise HTTPException(404)
        items = await asyncio.to_thread(zabbix_call, "item.get", {
            "output": ["itemid", "hostid", "name", "units", "value_type", "status"],
            "hostids": [host_id], "itemids": [item_id],
        })
        item = next((row for row in items if str(row.get("itemid")) == item_id
                     and str(row.get("hostid")) == host_id
                     and str(row.get("status")) == "0"
                     and str(row.get("value_type")) in ("0", "3")), None)
        if item is None:
            raise HTTPException(404)
        history = await numeric_history({"id": item_id, "label": item["name"],
                                         "units": item.get("units") or "",
                                         "value_type": item["value_type"]}, period)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    return JSONResponse(history, headers={"Cache-Control": "no-store"})
