"""Read the exact Cisco MAC State using a targeted, read-only RSH command."""
import asyncio
import errno
import getpass
import ipaddress
import os
import re
import secrets
import socket

from cmts_monitor import normalize_mac

MAX_OUTPUT = 65536
TIMEOUT = 4
_ROW = re.compile(
    r"^\s*([0-9a-fA-F]{4}(?:\.[0-9a-fA-F]{4}){2})\s+"
    r"(?:\d{1,3}\.){3}\d{1,3}\s+\S+\s+"
    r"(#?[a-zA-Z][a-zA-Z0-9_-]*(?:\([a-zA-Z0-9_-]+\))?)(?=\s|$)"
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


def _local_user():
    try:
        import pwd
    except ImportError:
        return getpass.getuser()
    return pwd.getpwuid(os.getuid()).pw_name


def _diagnostic(output):
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", output.decode("utf-8", "replace"))
    return " ".join("".join(char for char in text
                            if char.isprintable() or char.isspace()).split())[:500]


async def _connect(address):
    """Use a reserved client port, as required by the RSH server."""
    family = socket.AF_INET6 if ipaddress.ip_address(address).version == 6 else socket.AF_INET
    local_address = "::" if family == socket.AF_INET6 else "0.0.0.0"
    start = secrets.randbelow(512)
    for offset in range(512):
        port = 512 + (start + offset) % 512
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setblocking(False)
            sock.bind((local_address, port))
            await asyncio.get_running_loop().sock_connect(sock, (address, 514))
            return await asyncio.open_connection(sock=sock)
        except OSError as exc:
            sock.close()
            if exc.errno in (errno.EADDRINUSE, errno.EADDRNOTAVAIL):
                continue
            if exc.errno in (errno.EACCES, errno.EPERM):
                raise RuntimeError("RSH: нет права привязать исходящий порт 512–1023. "
                                   "Проверьте sysctl net.ipv4.ip_unprivileged_port_start=512 "
                                   "в настройках контейнера.") from exc
            raise
        except BaseException:
            sock.close()
            raise
    raise RuntimeError("RSH: все исходящие порты 512–1023 заняты.")


async def _exchange(address, local_user, remote_user, command):
    reader, writer = await _connect(address)
    try:
        # Port 0 disables the reverse stderr connection. Both streams use this
        # one outgoing TCP session, so the server never connects back through NAT.
        payload = b"0\0" + b"\0".join(value.encode("ascii") for value in
                                       (local_user, remote_user, command)) + b"\0"
        writer.write(payload)
        await writer.drain()
        ack = await reader.readexactly(1)
        if ack != b"\0":
            detail = await reader.read(500)
            raise RuntimeError("RSH: " + (_diagnostic(ack + detail) or "отказ CMTS."))
        chunks = []
        size = 0
        while True:
            chunk = await reader.read(min(4096, MAX_OUTPUT + 1 - size))
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_OUTPUT:
                raise RuntimeError("Ответ CLI превысил допустимый размер.")
    finally:
        writer.close()
        await writer.wait_closed()


async def read_status(cmts, mac, user=""):
    compact = normalize_mac(mac)
    if len(compact) != 12:
        raise ValueError("Нужен полный MAC модема.")
    address = str(ipaddress.ip_address(cmts["address"]))
    local_user = _local_user()
    remote_user = user or local_user
    for name in (local_user, remote_user):
        if not re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,63}", name):
            raise ValueError("Некорректное имя пользователя RSH.")
    dotted = ".".join(compact[i:i + 4] for i in range(0, 12, 4))
    try:
        output = await asyncio.wait_for(_exchange(address, local_user, remote_user,
                                                  "show cable modem " + dotted), TIMEOUT)
        try:
            return parse_status(output.decode("utf-8", "replace"), compact)
        except RuntimeError as exc:
            raise RuntimeError(str(exc) + " " + _diagnostic(output)) from exc
    except asyncio.TimeoutError as exc:
        raise RuntimeError("Таймаут чтения MAC State через RSH.") from exc
    except asyncio.IncompleteReadError as exc:
        raise RuntimeError("RSH: CMTS закрыла соединение до подтверждения запроса.") from exc
    except OSError as exc:
        raise RuntimeError("RSH: " + str(exc)) from exc
