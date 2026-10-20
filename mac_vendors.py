"""Offline MAC attribution using a bundled, atomically replaceable runZero snapshot."""
import gzip
import json
import os
import re
import tempfile
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from urllib import error, request as urlrequest

SOURCE_URL = "https://raw.githubusercontent.com/runZeroInc/mac-tracker/main/data/macs.json"
BUNDLED_PATH = Path(__file__).resolve().parent / "data" / "mac-vendors.json.gz"
MAX_SOURCE_BYTES = 64 * 1024 * 1024
MIN_PREFIXES = 10000
_prefix_pattern = re.compile(r"([0-9a-f]{12})/(24|28|36)\Z")
_cache_lock = threading.Lock()
_update_lock = threading.Lock()
_cached = None


def database_path():
    from app_config import DB_PATH
    return DB_PATH.parent / "mac-vendors.json.gz"


def _valid_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Некорректная дата в базе MAC.")
    date.fromisoformat(value)
    return value


def _prefix(value):
    match = _prefix_pattern.fullmatch(value)
    if not match:
        raise ValueError("Некорректный префикс в базе MAC.")
    digits = int(match[2]) // 4
    if any(char != "0" for char in match[1][digits:]):
        raise ValueError("Некорректная маска префикса MAC.")
    return match[1][:digits]


def snapshot_from_source(raw):
    """Keep the latest owner and earliest observed date, excluding deleted blocks."""
    if len(raw) > MAX_SOURCE_BYTES:
        raise ValueError("База MAC превышает допустимый размер.")
    source = json.loads(raw)
    if not isinstance(source, dict) or not 1 <= len(source) <= 200000:
        raise ValueError("Неверный формат базы runZero.")
    prefixes = {}
    for block, history in source.items():
        prefix = _prefix(block)
        if not isinstance(history, list) or not history:
            raise ValueError("Нет истории префикса MAC.")
        for record in history:
            if not isinstance(record, dict) or record.get("t") not in ("add", "change", "delete"):
                raise ValueError("Неверная запись истории MAC.")
            _valid_date(record.get("d"))
            if not isinstance(record.get("o"), str) or len(record["o"]) > 1000:
                raise ValueError("Неверное имя производителя MAC.")
        ordered = sorted(history, key=lambda record: record["d"])
        latest = ordered[-1]
        if latest["t"] == "delete":
            continue
        vendor = " ".join(latest["o"].split())
        prefixes[prefix] = [vendor, ordered[0]["d"]]
    if len(prefixes) < MIN_PREFIXES:
        raise ValueError("База MAC неполная: слишком мало префиксов.")
    return {"version": 1, "downloaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": SOURCE_URL, "prefixes": prefixes}


def _read_snapshot(path):
    with gzip.open(path, "rb") as stream:
        raw = stream.read(MAX_SOURCE_BYTES + 1)
    if len(raw) > MAX_SOURCE_BYTES:
        raise ValueError("Слишком большая база MAC.")
    data = json.loads(raw)
    if (not isinstance(data, dict) or data.get("version") != 1
            or data.get("source") != SOURCE_URL or not isinstance(data.get("prefixes"), dict)
            or not MIN_PREFIXES <= len(data["prefixes"]) <= 200000):
        raise ValueError("Неверный формат локальной базы MAC.")
    datetime.fromisoformat(data["downloaded_at"])
    for prefix, record in data["prefixes"].items():
        if (not re.fullmatch(r"[0-9a-f]{6}|[0-9a-f]{7}|[0-9a-f]{9}", prefix)
                or not isinstance(record, list) or len(record) != 2
                or not isinstance(record[0], str) or len(record[0]) > 1000):
            raise ValueError("Неверная запись локальной базы MAC.")
        _valid_date(record[1])
    return data


def _load():
    global _cached
    paths = (database_path(), BUNDLED_PATH)
    with _cache_lock:
        signature = tuple((str(path), path.stat().st_mtime_ns, path.stat().st_size)
                          if path.is_file() else (str(path), None, None) for path in paths)
        if _cached is not None and _cached[0] == signature:
            return _cached[1]
        errors = []
        for index, path in enumerate(paths):
            if not path.is_file():
                continue
            try:
                data = _read_snapshot(path)
            except (OSError, ValueError, KeyError, TypeError, EOFError):
                errors.append("Не удалось прочитать " + ("обновлённую" if index == 0 else "встроенную")
                              + " базу MAC.")
                continue
            result = (data, index == 1, " ".join(errors))
            _cached = (signature, result)
            return result
        result = (None, False, " ".join(errors) or "База MAC отсутствует.")
        _cached = (signature, result)
        return result


def database_status():
    data, bundled, error_message = _load()
    return {"count": len(data["prefixes"]) if data else 0,
            "downloaded_at": data["downloaded_at"][:10] if data else None,
            "bundled": bundled, "error": error_message}


def lookup_macs(addresses):
    data, _, _ = _load()
    results = []
    for address in addresses:
        compact = re.sub(r"[:.\-\s]", "", address).lower()
        result = {"mac": address, "vendor": None, "first_seen": None, "kind": "unknown"}
        if not re.fullmatch(r"[0-9a-f]{12}", compact):
            results.append(result)
            continue
        first = int(compact[:2], 16)
        if first & 1:
            result["kind"] = "multicast"
        elif first & 2:
            result["kind"] = "local"
        elif data:
            for digits in (9, 7, 6):
                record = data["prefixes"].get(compact[:digits])
                if record:
                    result.update(vendor=record[0], first_seen=record[1], kind="known")
                    break
        else:
            result["kind"] = "unavailable"
        results.append(result)
    return results


def update_database():
    """Download and validate before replacing the persistent copy; retain it on failure."""
    if not _update_lock.acquire(blocking=False):
        raise RuntimeError("Обновление базы MAC уже выполняется.")
    temporary = None
    try:
        request = urlrequest.Request(SOURCE_URL, headers={"User-Agent": "ZabbixTools-MAC/1.0"})
        with urlrequest.urlopen(request, timeout=30) as response:
            raw = response.read(MAX_SOURCE_BYTES + 1)
        data = snapshot_from_source(raw)
        path = database_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix="mac-vendors-", suffix=".tmp",
                                         delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(gzip.compress(json.dumps(data, ensure_ascii=False,
                                                  separators=(",", ":")).encode("utf-8"), mtime=0))
        os.replace(temporary, path)
        return len(data["prefixes"])
    except (OSError, ValueError, error.URLError) as exc:
        raise RuntimeError("Не удалось обновить базу MAC. Предыдущая копия сохранена.") from exc
    finally:
        try:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        finally:
            _update_lock.release()
