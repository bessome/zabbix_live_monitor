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
DEFAULT_ACTIVITY_RETENTION_DAYS = 90

def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
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
                email TEXT,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('read', 'execute', 'admin')),
                active INTEGER NOT NULL DEFAULT 1,
                theme TEXT NOT NULL DEFAULT 'light' CHECK (theme IN ('light', 'dark'))
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS cmts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                address TEXT NOT NULL UNIQUE,
                port INTEGER NOT NULL DEFAULT 161 CHECK (port BETWEEN 1 AND 65535),
                read_community TEXT
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
            CREATE INDEX IF NOT EXISTS activity_log_expiry ON activity_log(occurred_at);
            CREATE TABLE IF NOT EXISTS favorites (
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                host_id TEXT NOT NULL,
                category TEXT NOT NULL,
                name TEXT NOT NULL,
                address TEXT NOT NULL,
                added_at INTEGER NOT NULL,
                PRIMARY KEY (user_id, host_id)
            );
            CREATE INDEX IF NOT EXISTS favorites_user_recent
                ON favorites(user_id, added_at DESC);
            CREATE TABLE IF NOT EXISTS recent_devices (
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                host_id TEXT NOT NULL,
                category TEXT NOT NULL,
                name TEXT NOT NULL,
                address TEXT NOT NULL,
                viewed_at INTEGER NOT NULL,
                PRIMARY KEY (user_id, host_id)
            );
            CREATE INDEX IF NOT EXISTS recent_devices_user_recent
                ON recent_devices(user_id, viewed_at DESC);
        """)
        columns = {row["name"] for row in con.execute("PRAGMA table_info(users)")}
        if "theme" not in columns:
            con.execute("ALTER TABLE users ADD COLUMN theme TEXT NOT NULL DEFAULT 'light' "
                        "CHECK (theme IN ('light', 'dark'))")
        if "email" not in columns:
            con.execute("ALTER TABLE users ADD COLUMN email TEXT")
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_email_unique "
                    "ON users(email COLLATE NOCASE) WHERE email IS NOT NULL")
        if con.execute("SELECT id FROM users WHERE role='admin' LIMIT 1").fetchone() is None:
            password = os.environ.get("ADMIN_PASSWORD", "")
            if len(password) < 9:
                raise RuntimeError("Set ADMIN_PASSWORD (at least 9 characters) for first start")
            con.execute("INSERT INTO users(username,password_hash,role) VALUES (?,?,'admin')",
                        ("Admin", hash_password(password)))
        con.commit()


def purge_expired_activity(con):
    """Apply the configured retention period inside the caller's transaction."""
    row = con.execute(
        "SELECT value FROM settings WHERE key='activity_retention_days'"
    ).fetchone()
    days = int(row["value"]) if row else DEFAULT_ACTIVITY_RETENTION_DAYS
    if days:
        con.execute("DELETE FROM activity_log WHERE occurred_at < ?",
                    (int(time.time()) - days * 86400,))


def record_activity(user, event, section, path):
    """Persist a successful sign-in or a user-visible page visit."""
    with closing(connect()) as con:
        purge_expired_activity(con)
        con.execute(
            "INSERT INTO activity_log(occurred_at,user_id,username,event,section,path) "
            "VALUES (?,?,?,?,?,?)",
            (int(time.time()), user["id"], user["username"], event, section, path),
        )
        con.commit()


def favorite_ids_for(user_id):
    with closing(connect()) as con:
        rows = con.execute("SELECT host_id FROM favorites WHERE user_id=?", (user_id,))
        return {row["host_id"] for row in rows}


def favorite_devices_for(user_id):
    with closing(connect()) as con:
        rows = con.execute(
            "SELECT host_id,category,name,address FROM favorites WHERE user_id=? "
            "ORDER BY added_at DESC, host_id", (user_id,)).fetchall()
        return [dict(row) for row in rows]


def recent_devices_for(user_id):
    with closing(connect()) as con:
        rows = con.execute(
            "SELECT host_id,category,name,address FROM recent_devices WHERE user_id=? "
            "ORDER BY viewed_at DESC LIMIT 15", (user_id,)).fetchall()
        return [dict(row) for row in rows]


def save_favorite(user_id, device, category):
    with closing(connect()) as con:
        con.execute(
            "INSERT INTO favorites(user_id,host_id,category,name,address,added_at) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(user_id,host_id) DO UPDATE SET "
            "category=excluded.category,name=excluded.name,address=excluded.address",
            (user_id, device["id"], category, device["name"], device["address"],
             time.time_ns()),
        )
        con.commit()


def remove_favorite(user_id, host_id):
    with closing(connect()) as con:
        con.execute("DELETE FROM favorites WHERE user_id=? AND host_id=?",
                    (user_id, host_id))
        con.commit()


def record_recent_device(user_id, device, category):
    with closing(connect()) as con:
        con.execute(
            "INSERT INTO recent_devices(user_id,host_id,category,name,address,viewed_at) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(user_id,host_id) DO UPDATE SET "
            "category=excluded.category,name=excluded.name,address=excluded.address,"
            "viewed_at=excluded.viewed_at",
            (user_id, device["id"], category, device["name"], device["address"],
             time.time_ns()),
        )
        con.execute(
            "DELETE FROM recent_devices WHERE user_id=? AND host_id NOT IN "
            "(SELECT host_id FROM recent_devices WHERE user_id=? "
            "ORDER BY viewed_at DESC LIMIT 15)", (user_id, user_id),
        )
        con.commit()


def setting(key, default=""):
    with closing(connect()) as con:
        row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def snmp_community_for(category):
    return _snmp_community_for(category, "snmp_community:", "public")


def snmp_write_community_for(category):
    return _snmp_community_for(category, "snmp_write_community:", "private")


def _snmp_community_for(category, prefix, default):
    if category not in CATEGORIES:
        raise HTTPException(404)
    encrypted = setting(prefix + category)
    if not encrypted:
        return default
    try:
        return _community_cipher.decrypt(encrypted.encode()).decode()
    except (InvalidToken, UnicodeDecodeError) as exc:
        raise RuntimeError("Не удалось прочитать SNMP community. Задайте её заново в настройках.") from exc


def encrypt_community(community):
    return _community_cipher.encrypt(community.encode()).decode()


def cmts_list():
    with closing(connect()) as con:
        rows = con.execute("SELECT id,name,address,port,read_community FROM cmts "
                           "ORDER BY name COLLATE NOCASE").fetchall()
    return [{"id": row["id"], "name": row["name"],
             "address": row["address"], "port": row["port"],
             "community_configured": bool(row["read_community"])} for row in rows]


def cmts_for(cmts_id):
    with closing(connect()) as con:
        row = con.execute("SELECT id,name,address,port,read_community FROM cmts WHERE id=?",
                          (cmts_id,)).fetchone()
    if row is None:
        return None
    result = dict(row)
    if result["read_community"]:
        try:
            result["community"] = _community_cipher.decrypt(
                result["read_community"].encode()).decode()
        except (InvalidToken, UnicodeDecodeError) as exc:
            raise RuntimeError("Не удалось прочитать SNMP community CMTS. Задайте её заново.") from exc
    else:
        result["community"] = "public"
    del result["read_community"]
    return result
