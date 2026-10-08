"""Configured Cisco CMTS search and discovered modem live card."""
import asyncio
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app_storage import cmts_for, cmts_list, snmp_community_for
from app_web import render, require_user
from cmts_monitor import normalize_mac, resolve_mac, search_cmts
from docsis_modem import basic_channels
from ping_monitor import snapshot_for
from snmp_monitor import uptime_for

router = APIRouter()
_verified = {}
_verify_lock = asyncio.Lock()
VERIFY_SECONDS = 30
_search_limit = asyncio.Semaphore(4)


async def verified_modem(cmts_id, mac):
    try:
        compact = normalize_mac(mac)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if len(compact) != 12:
        raise HTTPException(422, "Нужен полный MAC.")
    cmts = cmts_for(cmts_id)
    if cmts is None:
        raise HTTPException(404)
    key = (cmts_id, compact)
    async with _verify_lock:
        cached = _verified.get(key)
        if cached and time.monotonic() - cached[0] < VERIFY_SECONDS:
            return cached[1]
    try:
        modem = await resolve_mac(cmts, compact)
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    if modem is None:
        raise HTTPException(404, "Модем не найден на CMTS.")
    async with _verify_lock:
        if len(_verified) > 500:
            _verified.clear()
        _verified[key] = (time.monotonic(), modem)
    return modem


@router.get("/api/cmts/search")
async def cmts_search(request: Request, q: str = "", cmts_id: int = 0):
    require_user(request, api=True)
    try:
        query = normalize_mac(q)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    configs = cmts_list()
    if cmts_id:
        configs = [item for item in configs if item["id"] == cmts_id]
        if not configs:
            raise HTTPException(404, "CMTS не найдена.")
    if not configs:
        raise HTTPException(422, "Сначала добавьте CMTS в настройках.")
    targets = []
    errors = []
    for item in configs:
        try:
            targets.append(cmts_for(item["id"]))
        except RuntimeError as exc:
            errors.append({"cmts": item["name"], "error": str(exc)})
    if not targets:
        return JSONResponse({"results": [], "errors": errors, "truncated": False},
                            status_code=503)
    # A wide fragment can match thousands of modems: bound the work and output.
    async def search_one(cmts):
        async with _search_limit:
            try:
                return await search_cmts(cmts, query)
            except (RuntimeError, OSError, ValueError) as exc:
                return exc
    outcomes = await asyncio.gather(*(search_one(cmts) for cmts in targets))
    rows = []
    truncated = False
    for cmts, outcome in zip(targets, outcomes):
        if isinstance(outcome, Exception):
            errors.append({"cmts": cmts["name"], "error": str(outcome)})
        else:
            found, limited = outcome
            rows.extend(found)
            truncated = truncated or limited
    rows.sort(key=lambda row: (row["cmts_name"].casefold(), row["mac_compact"]))
    if len(rows) > 200:
        rows = rows[:200]
        truncated = True
    return JSONResponse({"results": rows, "errors": errors, "truncated": truncated},
                        headers={"Cache-Control": "no-store"})


@router.get("/cmts/{cmts_id}/modems/{mac}")
async def discovered_modem_page(request: Request, cmts_id: int, mac: str):
    require_user(request)
    modem = await verified_modem(cmts_id, mac)
    return render(request, "discovered_modem.html", category="Modems",
                  modem=modem)


@router.get("/api/cmts/{cmts_id}/modems/{mac}/ping")
async def discovered_ping(request: Request, cmts_id: int, mac: str):
    require_user(request, api=True)
    modem = await verified_modem(cmts_id, mac)
    if not modem["ip"]:
        raise HTTPException(404, "IP модема неизвестен.")
    try:
        result = snapshot_for("cmts:" + str(cmts_id) + ":" + modem["mac_compact"],
                              modem["ip"])
    except (RuntimeError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.get("/api/cmts/{cmts_id}/modems/{mac}/uptime")
async def discovered_uptime(request: Request, cmts_id: int, mac: str):
    require_user(request, api=True)
    modem = await verified_modem(cmts_id, mac)
    if not modem["ip"]:
        raise HTTPException(404)
    try:
        value = await uptime_for("cmts:" + str(cmts_id) + ":" + modem["mac_compact"],
                                 modem["ip"], 161, snmp_community_for("Modems"))
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    return JSONResponse({"seconds": value}, headers={"Cache-Control": "no-store"})


@router.get("/api/cmts/{cmts_id}/modems/{mac}/modem-channels")
async def discovered_channels(request: Request, cmts_id: int, mac: str):
    require_user(request, api=True)
    modem = await verified_modem(cmts_id, mac)
    if not modem["ip"]:
        raise HTTPException(404)
    try:
        result = await basic_channels(modem["ip"], snmp_community_for("Modems"))
    except (RuntimeError, ValueError) as exc:
        return JSONResponse({"error": str(exc), "items": []}, status_code=503)
    return JSONResponse(result, headers={"Cache-Control": "no-store"})
