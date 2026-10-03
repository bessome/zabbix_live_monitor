"""SQLite settings, users and encrypted SNMP credentials."""
import base64
import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from contextlib import closing

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException

from app_config import CATEGORIES, DB_PATH, SECRET

_community_cipher = Fernet(base64.urlsafe_b64encode(hashlib.sha256(SECRET.encode()).digest()))

def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600000)
    return salt.hex() + ":" + digest.hex()


def check_password(password, stored):
    try:
        salt_hex, digest_hex = stored.split(":", 1)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(),
                                    bytes.fromhex(salt_hex), 600000)
        return hmac.compare_digest(digest, bytes.fromhex(digest_hex))
    except ValueError:
        return False


def init_db():
    with closing(connect()) as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('read', 'execute', 'admin')),
                active INTEGER NOT NULL DEFAULT 1,
                theme TEXT NOT NULL DEFAULT 'light' CHECK (theme IN ('light', 'dark'))
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS activity_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                occurred_at INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                event TEXT NOT NULL CHECK (event IN ('login', 'view', 'logout')),
                section TEXT NOT NULL,
                path TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS activity_log_recent ON activity_log(id DESC);
        """)
        columns = {row["name"] for row in con.execute("PRAGMA table_info(users)")}
        if "theme" not in columns:
            con.execute("ALTER TABLE users ADD COLUMN theme TEXT NOT NULL DEFAULT 'light' "
                        "CHECK (theme IN ('light', 'dark'))")
        if con.execute("SELECT id FROM users WHERE role='admin' LIMIT 1").fetchone() is None:
            password = os.environ.get("ADMIN_PASSWORD", "")
            if len(password) < 12:
                raise RuntimeError("Set ADMIN_PASSWORD (at least 12 characters) for first start")
            con.execute("INSERT INTO users(username,password_hash,role) VALUES (?,?,'admin')",
                        ("Admin", hash_password(password)))
        con.commit()


def record_activity(user, event, section, path):
    """Persist a successful sign-in or a user-visible page visit."""
    with closing(connect()) as con:
        con.execute(
            "INSERT INTO activity_log(occurred_at,user_id,username,event,section,path) "
            "VALUES (?,?,?,?,?,?)",
            (int(time.time()), user["id"], user["username"], event, section, path),
        )
        con.commit()


def setting(key, default=""):
    with closing(connect()) as con:
        row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def snmp_community_for(category):
    if category not in CATEGORIES:
        raise HTTPException(404)
    encrypted = setting("snmp_community:" + category)
    if not encrypted:
        return "public"
    try:
        return _community_cipher.decrypt(encrypted.encode()).decode()
    except (InvalidToken, UnicodeDecodeError) as exc:
        raise RuntimeError("Не удалось прочитать SNMP community. Задайте её заново в настройках.") from exc


def encrypt_community(community):
    return _community_cipher.encrypt(community.encode()).decode()
