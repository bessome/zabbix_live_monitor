"""Administrative settings, Zabbix catalog and user management."""
import asyncio
import os
import sqlite3
from contextlib import closing
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app_config import CATEGORIES
from app_storage import (check_password, connect, encrypt_community,
                         hash_password, setting)
from app_web import add_message, checked_form, render, require_admin
from zabbix_service import category_filter, clear_caches, zabbix_call, zabbix_catalog

router = APIRouter()

@router.get("/settings")
def settings_page(request: Request):
    require_admin(request)
    filters = {category: category_filter(category) for category in CATEGORIES}
    return render(request, "settings.html", zabbix_url=setting("zabbix_url"),
                   filters=filters,
                   snmp_configured={category: bool(setting("snmp_community:" + category))
                                    for category in CATEGORIES},
                   token_configured=bool(os.environ.get("ZABBIX_API_TOKEN")))


@router.get("/api/zabbix/catalog/{kind}")
async def catalog_api(request: Request, kind: str):
    require_admin(request)
    if kind not in ("group", "template"):
        raise HTTPException(404)
    try:
        items = await asyncio.to_thread(zabbix_catalog, kind)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    return JSONResponse({"items": items}, headers={"Cache-Control": "no-store"})


@router.post("/settings")
async def save_settings(request: Request):
    require_admin(request)
    form = await checked_form(request)
    url = str(form.get("zabbix_url", "")).strip()
    parsed = urlparse(url)
    if url and (parsed.scheme not in ("http", "https") or not parsed.netloc
                or not parsed.path.endswith("/api_jsonrpc.php")):
        add_message(request, "Укажите полный URL вида https://host/zabbix/api_jsonrpc.php.", "error")
        return RedirectResponse("/settings", status_code=303)
    filters = {}
    communities = {}
    for category in CATEGORIES:
        mode = str(form.get("mode:" + category, "group"))
        ids = [item.strip() for item in str(form.get("ids:" + category, "")).split(",")
               if item.strip()]
        if mode not in ("group", "template") or any(not item.isdigit() for item in ids):
            add_message(request, "Неверный фильтр для " + category + ": нужны числовые ID.", "error")
            return RedirectResponse("/settings", status_code=303)
        filters[category] = (mode, ",".join(ids))
        community = str(form.get("snmp_community:" + category, ""))
        if len(community) > 128 or any(ord(char) < 32 for char in community):
            add_message(request, "Неверная SNMP community для " + category + ".", "error")
            return RedirectResponse("/settings", status_code=303)
        if form.get("clear_snmp:" + category) == "1":
            communities[category] = None
        elif community:
            communities[category] = encrypt_community(community)
    with closing(connect()) as con:
        con.execute("INSERT INTO settings(key,value) VALUES ('zabbix_url',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (url,))
        for category, (mode, ids) in filters.items():
            for key, value in (("filter_mode:" + category, mode),
                               ("filter_ids:" + category, ids)):
                con.execute("INSERT INTO settings(key,value) VALUES (?,?) "
                            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                             (key, value))
        for category, encrypted in communities.items():
            key = "snmp_community:" + category
            if encrypted is None:
                con.execute("DELETE FROM settings WHERE key=?", (key,))
            else:
                con.execute("INSERT INTO settings(key,value) VALUES (?,?) "
                            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (key, encrypted))
        con.commit()
    clear_caches()
    add_message(request, "Настройки сохранены.")
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/check")
async def check_connection(request: Request):
    require_admin(request)
    await checked_form(request)
    try:
        await asyncio.to_thread(zabbix_call, "host.get", {"output": ["hostid"], "limit": 1})
        add_message(request, "Соединение с Zabbix API работает.")
    except RuntimeError as exc:
        add_message(request, str(exc), "error")
    return RedirectResponse("/settings", status_code=303)


@router.get("/settings/users")
def users_page(request: Request):
    require_admin(request)
    with closing(connect()) as con:
        users = con.execute("SELECT id,username,role,active FROM users ORDER BY username").fetchall()
    return render(request, "users.html", users=users)


@router.get("/settings/activity")
def activity_page(request: Request, page: int = 1, username: str = ""):
    require_admin(request)
    page = max(1, page)
    username = username.strip()[:80]
    where = "WHERE instr(lower(username), lower(?)) > 0" if username else ""
    params = (username,) if username else ()
    with closing(connect()) as con:
        total = con.execute("SELECT COUNT(*) FROM activity_log " + where, params).fetchone()[0]
        pages = max(1, (total + 199) // 200)
        page = min(page, pages)
        events = con.execute(
            "SELECT datetime(occurred_at, 'unixepoch') AS occurred_utc, "
            "username,event,section,path FROM activity_log "
            + where + " ORDER BY id DESC LIMIT 200 OFFSET ?",
            (*params, (page - 1) * 200)
        ).fetchall()
    return render(request, "activity.html", events=events, page=page,
                  pages=pages, total=total, username_filter=username)


@router.post("/settings/users")
async def create_user(request: Request):
    require_admin(request)
    form = await checked_form(request)
    username = str(form.get("username", "")).strip()
    password = str(form.get("password", ""))
    role = str(form.get("role", ""))
    if not username or len(username) > 80 or len(password) < 12 or role not in ("read", "execute"):
        add_message(request, "Укажите имя, роль и пароль длиной от 12 символов.", "error")
    else:
        try:
            with closing(connect()) as con:
                con.execute("INSERT INTO users(username,password_hash,role) VALUES (?,?,?)",
                            (username, hash_password(password), role))
                con.commit()
            add_message(request, "Пользователь создан.")
        except sqlite3.IntegrityError:
            add_message(request, "Такое имя пользователя уже существует.", "error")
    return RedirectResponse("/settings/users", status_code=303)


@router.post("/settings/users/{user_id}")
async def update_user(request: Request, user_id: int):
    require_admin(request)
    form = await checked_form(request)
    with closing(connect()) as con:
        target = con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not target:
            raise HTTPException(404)
        if target["role"] == "admin":
            raise HTTPException(403)
        role = str(form.get("role", ""))
        if role not in ("read", "execute"):
            raise HTTPException(400)
        password = str(form.get("password", ""))
        if password and len(password) < 12:
            add_message(request, "Новый пароль должен содержать не менее 12 символов.", "error")
            return RedirectResponse("/settings/users", status_code=303)
        active = int(form.get("active") == "1")
        con.execute("UPDATE users SET role=?,active=? WHERE id=?", (role, active, user_id))
        if password:
            con.execute("UPDATE users SET password_hash=? WHERE id=?",
                        (hash_password(password), user_id))
        con.commit()
    add_message(request, "Пользователь обновлён.")
    return RedirectResponse("/settings/users", status_code=303)


@router.post("/settings/admin-password")
async def change_admin_password(request: Request):
    admin = require_admin(request)
    form = await checked_form(request)
    current = str(form.get("current_password", ""))
    new = str(form.get("new_password", ""))
    if len(new) < 12:
        add_message(request, "Новый пароль должен содержать не менее 12 символов.", "error")
        return RedirectResponse("/settings/users", status_code=303)
    with closing(connect()) as con:
        row = con.execute("SELECT password_hash FROM users WHERE id=?", (admin["id"],)).fetchone()
        if not check_password(current, row["password_hash"]):
            add_message(request, "Текущий пароль неверен.", "error")
            return RedirectResponse("/settings/users", status_code=303)
        con.execute("UPDATE users SET password_hash=? WHERE id=?",
                    (hash_password(new), admin["id"]))
        con.commit()
    add_message(request, "Пароль Admin изменён.")
    return RedirectResponse("/settings/users", status_code=303)
