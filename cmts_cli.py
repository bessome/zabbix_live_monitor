"""Read the exact Cisco MAC State using a targeted, read-only RSH command."""
import asyncio
import ipaddress
import os
import re

from cmts_monitor import normalize_mac

MAX_OUTPUT = 65536
TIMEOUT = 4
_ROW = re.compile(
    r"^\s*([0-9a-fA-F]{4}(?:\.[0-9a-fA-F]{4}){2})\s+"
    r"(?:\d{1,3}\.){3}\d{1,3}\s+\S+\s+"
    r"([a-zA-Z][a-zA-Z0-9_-]*(?:\([a-zA-Z0-9_-]+\))?)(?=\s|$)"
)


def configuration():
    """No RSH connections unless the operator explicitly enables this source."""
    enabled = os.environ.get("CMTS_STATUS_RSH", "").strip() == "1"
    user = os.environ.get("CMTS_RSH_USER", "").strip()
    if enabled and user and not re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,63}", user):
        raise ValueError("Некорректное имя CMTS_RSH_USER.")
    return enabled, user


def parse_status(output, mac):
    compact = normalize_mac(mac)
    if len(compact) != 12:
        raise ValueError("Нужен полный MAC модема.")
    matches = []
    for line in output.splitlines():
        row = _ROW.match(line)
        if row and normalize_mac(row.group(1)) == compact:
            matches.append(row.group(2))
    if len(matches) != 1:
        raise RuntimeError("CLI не вернул единственную строку выбранного MAC.")
    return matches[0]


async def read_status(cmts, mac, user=""):
    compact = normalize_mac(mac)
    if len(compact) != 12:
        raise ValueError("Нужен полный MAC модема.")
    address = str(ipaddress.ip_address(cmts["address"]))
    if user and not re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,63}", user):
        raise ValueError("Некорректное имя CMTS_RSH_USER.")
    dotted = ".".join(compact[i:i + 4] for i in range(0, 12, 4))
    args = ["rsh"] + (["-l", user] if user else [])
    # No local shell. The remote command contains only a validated hexadecimal MAC.
    args += [address, "show cable modem " + dotted]
    try:
        process = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    except OSError as exc:
        raise RuntimeError("Не удалось запустить rsh. Проверьте наличие rsh-client.") from exc
    try:
        async def receive():
            chunks = []
            size = 0
            while True:
                chunk = await process.stdout.read(min(4096, MAX_OUTPUT + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_OUTPUT:
                    raise RuntimeError("Ответ CLI превысил допустимый размер.")
            await process.wait()
            return b"".join(chunks)

        output = await asyncio.wait_for(receive(), TIMEOUT)
        if process.returncode:
            diagnostic = output.decode("utf-8", "replace")
            diagnostic = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", diagnostic)
            diagnostic = " ".join("".join(char for char in diagnostic
                                          if char.isprintable() or char.isspace()).split())[:500]
            message = f"RSH завершился с кодом {process.returncode}."
            raise RuntimeError(message + (" " + diagnostic if diagnostic else " Нет текста ошибки."))
        return parse_status(output.decode("utf-8", "replace"), compact)
    except asyncio.TimeoutError as exc:
        raise RuntimeError("Таймаут чтения MAC State через RSH.") from exc
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
