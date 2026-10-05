"""Login, profile and health-check routes."""
import secrets
import sqlite3
import time
from contextlib import closing

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from app_storage import check_password, connect, hash_password, record_activity
from app_web import add_message, checked_form, render, require_user, user_for

router = APIRouter()

@router.get("/")
def index(request: Request):
    require_user(request)
    return render(request, "index.html")


@router.get("/healthz")
def healthz():
    try:
        with closing(connect()) as con:
            con.execute("SELECT 1").fetchone()
    except sqlite3.Error:
        raise HTTPException(503, "Database unavailable")
    return {"status": "ok"}


@router.get("/login")
def login_page(request: Request):
    if user_for(request):
        return RedirectResponse("/", status_code=303)
    return render(request, "login.html")


@router.post("/login")
async def login(request: Request):
    form = await checked_form(request)
    identifier = str(form.get("username", "")).strip()
    with closing(connect()) as con:
        user = con.execute(
            "SELECT * FROM users WHERE active=1 AND "
            "(username=? OR email COLLATE NOCASE=?)",
            (identifier, identifier)).fetchone()
    if user and check_password(str(form.get("password", "")), user["password_hash"]):
        request.session.clear()
        request.session["user_id"] = user["id"]
        request.session["login_at"] = int(time.time())
        request.session["csrf_token"] = secrets.token_urlsafe(32)
        record_activity(user, "login", "Вход", "/login")
        return RedirectResponse("/", status_code=303)
    add_message(request, "Неверное имя пользователя или пароль.", "error")
    return RedirectResponse("/login", status_code=303)


@router.post("/logout")
async def logout(request: Request):
    user = require_user(request)
    await checked_form(request)
    record_activity(user, "logout", "Выход", "/logout")
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@router.get("/profile")
def profile_page(request: Request):
    require_user(request)
    return render(request, "profile.html")


@router.post("/profile")
async def save_profile(request: Request):
    user = require_user(request)
    form = await checked_form(request)
    theme = str(form.get("theme", ""))
    if theme not in ("light", "dark"):
        raise HTTPException(400, "Invalid theme")
    with closing(connect()) as con:
        con.execute("UPDATE users SET theme=? WHERE id=?", (theme, user["id"]))
        con.commit()
    add_message(request, "Тема сохранена.")
    return RedirectResponse("/profile", status_code=303)


@router.post("/profile/password")
async def change_own_password(request: Request):
    user = require_user(request)
    form = await checked_form(request)
    current = str(form.get("current_password", ""))
    new = str(form.get("new_password", ""))
    confirmation = str(form.get("new_password_confirm", ""))
    if len(new) < 9:
        add_message(request, "Новый пароль должен содержать не менее 9 символов.", "error")
        return RedirectResponse("/profile", status_code=303)
    if new != confirmation:
        add_message(request, "Пароли не совпадают.", "error")
        return RedirectResponse("/profile", status_code=303)
    with closing(connect()) as con:
        stored = con.execute("SELECT password_hash FROM users WHERE id=?", (user["id"],)).fetchone()
        if not stored or not check_password(current, stored["password_hash"]):
            add_message(request, "Текущий пароль неверен.", "error")
            return RedirectResponse("/profile", status_code=303)
        con.execute("UPDATE users SET password_hash=? WHERE id=?",
                    (hash_password(new), user["id"]))
        con.commit()
    add_message(request, "Пароль изменён.")
    return RedirectResponse("/profile", status_code=303)
