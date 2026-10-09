"""Session authorization and shared template rendering."""
import hmac
import secrets
import time
from contextlib import closing

from fastapi import HTTPException
from fastapi.templating import Jinja2Templates

from app_config import CATEGORIES, DISPLAY_TIMEZONE, ROOT
from app_storage import connect, record_activity

templates = Jinja2Templates(directory=ROOT / "templates")
SESSION_SECONDS = 7 * 24 * 60 * 60

def user_for(request):
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    login_at = request.session.get("login_at")
    if not isinstance(login_at, (int, float)) or time.time() - login_at >= SESSION_SECONDS:
        request.session.clear()
        return None
    with closing(connect()) as con:
        user = con.execute("SELECT id,username,role,theme FROM users WHERE id=? AND active=1",
                           (user_id,)).fetchone()
    if user is None:
        request.session.clear()
    return user


def require_user(request, api=False):
    user = user_for(request)
    if user is None:
        if api:
            raise HTTPException(401, "Authentication required")
        raise HTTPException(303, headers={"Location": "/login"})
    return user


def require_admin(request):
    user = require_user(request)
    if user["role"] != "admin":
        raise HTTPException(403)
    return user


def add_message(request, text, kind="success"):
    request.session.setdefault("messages", []).append({"text": text, "kind": kind})


def render(request, name, **context):
    token = request.session.setdefault("csrf_token", secrets.token_urlsafe(32))
    current_user = user_for(request)
    if current_user is not None and request.method == "GET":
        path = request.url.path
        section = {
            "/": "Главная", "/favorites": "Избранное и история",
            "/profile": "Мои настройки",
            "/settings": "Настройки приложения",
            "/settings/cmts": "CMTS",
            "/settings/users": "Пользователи",
            "/settings/activity": "Журнал посещений",
        }.get(path)
        if section is None and path.startswith("/devices/"):
            category = path.split("/")[2]
            device = context.get("device")
            section = category if device is None else f"{category} / {device['name']}"
        if section is None and path.startswith("/cmts/"):
            section = "Modems / поиск CMTS"
        if section is None and path == "/onu-ont":
            section = "ONU/ONT"
        record_activity(current_user, "view", (section or path)[:160], path[:300])
    return templates.TemplateResponse(
        request=request, name=name,
        context={"categories": CATEGORIES, "current_user": current_user,
                 "display_timezone": DISPLAY_TIMEZONE,
                 "asset_version": max(file.stat().st_mtime_ns
                                      for file in (ROOT / "static").iterdir() if file.is_file()),
                 "csrf_token": token, "messages": request.session.pop("messages", []),
                 **context})


async def checked_form(request):
    form = await request.form()
    expected = request.session.get("csrf_token", "")
    supplied = str(form.get("csrf_token", ""))
    if not expected or not hmac.compare_digest(expected, supplied):
        raise HTTPException(400, "Invalid CSRF token")
    return form
