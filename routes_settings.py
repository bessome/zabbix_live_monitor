"""Administrative settings, Zabbix catalog and user management."""
import asyncio
import ipaddress
import os
import re
import sqlite3
from contextlib import closing
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app_config import CATEGORIES, format_display_timestamp
from app_storage import (DEFAULT_ACTIVITY_RETENTION_DAYS, check_password,
                         cmts_list, connect, encrypt_community, hash_password,
                         purge_expired_activity, setting)
from app_web import add_message, checked_form, render, require_admin
from switch_monitor import DEFAULT_EXCLUDED_NAMES, parse_excluded_names
from zabbix_service import category_filter, clear_caches, zabbix_call, zabbix_catalog

EMAIL_PATTERN = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+\Z")


def normalized_email(value):
    email = str(value).strip().casefold()
    return email if len(email) <= 254 and EMAIL_PATTERN.fullmatch(email) else None


def identifier_in_use(con, username, email, excluding_id=-1):
    return con.execute(
        "SELECT 1 FROM users WHERE id<>? AND "
        "(username COLLATE NOCASE IN (?,?) OR email COLLATE NOCASE IN (?,?)) "
        "LIMIT 1", (excluding_id, username, email, username, email),
    ).fetchone() is not None

router = APIRouter()

@router.get("/settings/cmts")
def cmts_page(request: Request):
    require_admin(request)
    return render(request, "cmts_settings.html", cmts=cmts_list())


@router.post("/settings/cmts")
async def save_cmts(request: Request):
    require_admin(request)
    form = await checked_form(request)
    name = str(form.get("name", "")).strip()
    address = str(form.get("address", "")).strip()
    community = str(form.get("community", ""))
    try:
        port = int(str(form.get("port", "161")))
        try:
            address = str(ipaddress.ip_address(address))
        except ValueError as exc:
            raise ValueError("Укажите корректный IP-адрес CMTS.") from exc
        if not name or len(name) > 80 or any(ord(c) < 32 for c in name):
            raise ValueError("Укажите имя CMTS до 80 символов.")
        if not 1 <= port <= 65535:
            raise ValueError("Порт SNMP должен быть от 1 до 65535.")
        if len(community) > 128 or any(ord(c) < 32 for c in community):
            raise ValueError("Неверная SNMP community.")
    except ValueError as exc:
        add_message(request, str(exc) if str(exc) else "Неверный адрес CMTS.", "error")
        return RedirectResponse("/settings/cmts", status_code=303)
    raw_id = str(form.get("cmts_id", "")).strip()
    if raw_id and not raw_id.isdecimal():
        raise HTTPException(400)
    encrypted = encrypt_community(community) if community else None
    try:
        with closing(connect()) as con:
            if raw_id:
                if not con.execute("SELECT 1 FROM cmts WHERE id=?", (int(raw_id),)).fetchone():
                    raise HTTPException(404)
                con.execute(
                    "UPDATE cmts SET name=?,address=?,port=?,"
                    "read_community=CASE WHEN ? THEN NULL ELSE COALESCE(?,read_community) END "
                    "WHERE id=?",
                    (name, address, port, form.get("clear_community") == "1",
                     encrypted, int(raw_id)))
            else:
                con.execute(
                    "INSERT INTO cmts(name,address,port,read_community) VALUES (?,?,?,?)",
                    (name, address, port, encrypted))
            con.commit()
    except sqlite3.IntegrityError:
        add_message(request, "CMTS с таким именем или адресом уже добавлена.", "error")
        return RedirectResponse("/settings/cmts", status_code=303)
    add_message(request, "CMTS сохранена.")
    return RedirectResponse("/settings/cmts", status_code=303)


@router.post("/settings/cmts/{cmts_id}/delete")
async def delete_cmts(request: Request, cmts_id: int):
    require_admin(request)
    await checked_form(request)
    with closing(connect()) as con:
        con.execute("DELETE FROM cmts WHERE id=?", (cmts_id,))
        con.commit()
    add_message(request, "CMTS удалена.")
    return RedirectResponse("/settings/cmts", status_code=303)


@router.get("/settings")
def settings_page(request: Request):
    require_admin(request)
    filters = {category: category_filter(category) for category in CATEGORIES}
    return render(request, "settings.html", zabbix_url=setting("zabbix_url"),
                   filters=filters,
                   switch_port_exclude=setting("switch_port_exclude", DEFAULT_EXCLUDED_NAMES),
                   activity_retention_days=int(setting(
                       "activity_retention_days", str(DEFAULT_ACTIVITY_RETENTION_DAYS))),
                   snmp_configured={category: bool(setting("snmp_community:" + category))
                                     for category in CATEGORIES},
                   snmp_write_configured={category: bool(setting(
                       "snmp_write_community:" + category)) for category in CATEGORIES},
                   token_configured=bool(os.environ.get("ZABBIX_API_TOKEN")))


@router.post("/settings/activity-retention")
async def save_activity_retention(request: Request):
    require_admin(request)
    form = await checked_form(request)
    raw = str(form.get("activity_retention_days", ""))
    if not raw.isdecimal() or not 0 <= int(raw) <= 3650:
        add_message(request, "Выберите срок хранения журнала от 1 до 3650 дней или без удаления.",
                    "error")
        return RedirectResponse("/settings", status_code=303)
    days = int(raw)
    with closing(connect()) as con:
        con.execute(
            "INSERT INTO settings(key,value) VALUES ('activity_retention_days',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(days),)
        )
        purge_expired_activity(con)
        con.commit()
    add_message(request, "Срок хранения журнала сохранён.")
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/switch-ports")
async def save_switch_port_filter(request: Request):
    require_admin(request)
    form = await checked_form(request)
    value = str(form.get("switch_port_exclude", "")).strip()
    try:
        parse_excluded_names(value)
    except ValueError as exc:
        add_message(request, str(exc), "error")
        return RedirectResponse("/settings", status_code=303)
    with closing(connect()) as con:
        con.execute(
            "INSERT INTO settings(key,value) VALUES ('switch_port_exclude',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (value,)
        )
        con.commit()
    add_message(request, "Фильтр портов Switches сохранён.")
    return RedirectResponse("/settings", status_code=303)


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
            communities["snmp_community:" + category] = None
        elif community:
            communities["snmp_community:" + category] = encrypt_community(community)
        write_community = str(form.get("snmp_write_community:" + category, ""))
        if len(write_community) > 128 or any(ord(char) < 32 for char in write_community):
            add_message(request, "Неверная SNMP write community для " + category + ".", "error")
            return RedirectResponse("/settings", status_code=303)
        if form.get("clear_snmp_write:" + category) == "1":
            communities["snmp_write_community:" + category] = None
        elif write_community:
            communities["snmp_write_community:" + category] = encrypt_community(write_community)
    with closing(connect()) as con:
        con.execute("INSERT INTO settings(key,value) VALUES ('zabbix_url',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (url,))
        for category, (mode, ids) in filters.items():
            for key, value in (("filter_mode:" + category, mode),
                               ("filter_ids:" + category, ids)):
                con.execute("INSERT INTO settings(key,value) VALUES (?,?) "
                            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                             (key, value))
        for key, encrypted in communities.items():
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
        users = con.execute(
            "SELECT id,username,email,role,active FROM users ORDER BY username").fetchall()
    admin_user = next((user for user in users if user["role"] == "admin"), None)
    return render(request, "users.html", users=users, admin_user=admin_user,
                  missing_email_count=sum(not user["email"] for user in users))


@router.get("/settings/activity")
def activity_page(request: Request, page: int = 1, username: str = ""):
    require_admin(request)
    page = max(1, page)
    username = username.strip()[:80]
    where = "WHERE instr(lower(username), lower(?)) > 0" if username else ""
    params = (username,) if username else ()
    with closing(connect()) as con:
        purge_expired_activity(con)
        con.commit()
        total = con.execute("SELECT COUNT(*) FROM activity_log " + where, params).fetchone()[0]
        pages = max(1, (total + 199) // 200)
        page = min(page, pages)
        rows = con.execute(
            "SELECT occurred_at,username,event,section,path FROM activity_log "
            + where + " ORDER BY id DESC LIMIT 200 OFFSET ?",
            (*params, (page - 1) * 200)
        ).fetchall()
    events = [{**dict(row), "occurred_local": format_display_timestamp(row["occurred_at"])}
              for row in rows]
    return render(request, "activity.html", events=events, page=page,
                  pages=pages, total=total, username_filter=username)


@router.post("/settings/users")
async def create_user(request: Request):
    require_admin(request)
    form = await checked_form(request)
    username = str(form.get("username", "")).strip()
    email = normalized_email(form.get("email", ""))
    password = str(form.get("password", ""))
    password_confirm = str(form.get("password_confirm", ""))
    role = str(form.get("role", ""))
    if (not username or len(username) > 80 or email is None
            or len(password) < 9 or role not in ("read", "execute")):
        add_message(request, "Укажите имя, email, роль и пароль длиной от 9 символов.", "error")
    elif password != password_confirm:
        add_message(request, "Пароли не совпадают.", "error")
    else:
        try:
            with closing(connect()) as con:
                if identifier_in_use(con, username, email):
                    add_message(request, "Имя или email уже используется.", "error")
                    return RedirectResponse("/settings/users", status_code=303)
                con.execute("INSERT INTO users(username,email,password_hash,role) "
                            "VALUES (?,?,?,?)",
                            (username, email, hash_password(password), role))
                con.commit()
            add_message(request, "Пользователь создан.")
        except sqlite3.IntegrityError:
            add_message(request, "Имя или email уже используется.", "error")
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
        email = normalized_email(form.get("email", ""))
        if email is None:
            add_message(request, "Укажите корректный email.", "error")
            return RedirectResponse("/settings/users", status_code=303)
        if identifier_in_use(con, target["username"], email, user_id):
            add_message(request, "Имя или email уже используется.", "error")
            return RedirectResponse("/settings/users", status_code=303)
        password = str(form.get("password", ""))
        password_confirm = str(form.get("password_confirm", ""))
        if password and len(password) < 9:
            add_message(request, "Новый пароль должен содержать не менее 9 символов.", "error")
            return RedirectResponse("/settings/users", status_code=303)
        if password != password_confirm:
            add_message(request, "Пароли не совпадают.", "error")
            return RedirectResponse("/settings/users", status_code=303)
        active = int(form.get("active") == "1")
        try:
            con.execute("UPDATE users SET email=?,role=?,active=? WHERE id=?",
                        (email, role, active, user_id))
            if password:
                con.execute("UPDATE users SET password_hash=? WHERE id=?",
                            (hash_password(password), user_id))
            con.commit()
        except sqlite3.IntegrityError:
            add_message(request, "Имя или email уже используется.", "error")
            return RedirectResponse("/settings/users", status_code=303)
    add_message(request, "Пользователь обновлён.")
    return RedirectResponse("/settings/users", status_code=303)


@router.post("/settings/admin-email")
async def change_admin_email(request: Request):
    admin = require_admin(request)
    form = await checked_form(request)
    email = normalized_email(form.get("email", ""))
    if email is None:
        add_message(request, "Укажите корректный email.", "error")
        return RedirectResponse("/settings/users", status_code=303)
    with closing(connect()) as con:
        if identifier_in_use(con, admin["username"], email, admin["id"]):
            add_message(request, "Имя или email уже используется.", "error")
            return RedirectResponse("/settings/users", status_code=303)
        try:
            con.execute("UPDATE users SET email=? WHERE id=?", (email, admin["id"]))
            con.commit()
        except sqlite3.IntegrityError:
            add_message(request, "Имя или email уже используется.", "error")
            return RedirectResponse("/settings/users", status_code=303)
    add_message(request, "Email Admin сохранён.")
    return RedirectResponse("/settings/users", status_code=303)


@router.post("/settings/admin-password")
async def change_admin_password(request: Request):
    admin = require_admin(request)
    form = await checked_form(request)
    current = str(form.get("current_password", ""))
    new = str(form.get("new_password", ""))
    new_confirm = str(form.get("new_password_confirm", ""))
    if len(new) < 9:
        add_message(request, "Новый пароль должен содержать не менее 9 символов.", "error")
        return RedirectResponse("/settings/users", status_code=303)
    if new != new_confirm:
        add_message(request, "Пароли не совпадают.", "error")
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
