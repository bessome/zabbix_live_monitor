"""One-shot DOCSIS modem reset using the standard Cable Device MIB."""
import asyncio
import ipaddress
import threading
import time

from pysnmp.hlapi.v3arch.asyncio import (
    CommunityData, ContextData, ObjectIdentity, ObjectType, SnmpEngine,
    Udp6TransportTarget, UdpTransportTarget, set_cmd,
)
from pysnmp.proto.rfc1902 import Integer

from ping_monitor import validate_target

RESET_OID = "1.3.6.1.2.1.69.1.1.3.0"
COOLDOWN_SECONDS = 30
_recent = {}
_lock = threading.Lock()


class RebootBusy(Exception):
    """A reset was already attempted for this SNMP endpoint recently."""


async def logged_reboot(user, device, address, port, community, source, send):
    from app_storage import begin_modem_reboot, finish_modem_reboot
    log_id = begin_modem_reboot(user, device, address, port, source)
    try:
        result = await send(address, port, community)
    except RebootBusy:
        finish_modem_reboot(log_id, "blocked")
        raise
    except ValueError:
        finish_modem_reboot(log_id, "invalid")
        raise
    except RuntimeError:
        finish_modem_reboot(log_id, "rejected")
        raise
    except Exception:
        finish_modem_reboot(log_id, "unknown")
        raise
    finish_modem_reboot(log_id, result["outcome"])
    return result


def claim_reset(address, port):
    key = (address, int(port))
    now = time.monotonic()
    with _lock:
        for old_key, started in list(_recent.items()):
            if now - started >= COOLDOWN_SECONDS:
                del _recent[old_key]
        if key in _recent:
            raise RebootBusy("Команда уже отправлялась. Повторите через 30 секунд.")
        _recent[key] = now


async def reboot_via_cmts(user, configs, device=None, mac=None):
    from cmts_cli import configuration, reset_modem
    from modem_status import locate_for_reset
    enabled, cli_user = configuration()
    if not enabled:
        raise ValueError("Перезагрузка через CMTS требует CMTS_STATUS_RSH=1.")
    cmts, compact, ip = await locate_for_reset(configs, device=device, mac=mac)

    async def send(address, port, community):
        # Share the modem cooldown with SNMP, even though RSH targets the CMTS.
        claim_reset(ip, (device or {}).get("snmp_port", 161))
        return await reset_modem(cmts, ip, cli_user)

    label = ((device or {}).get("name") or compact) + " / " + ip
    return await logged_reboot(user, label, cmts["address"], 514, "",
                               "RSH / CMTS " + cmts["name"], send)


async def reboot_modem(address, port, community):
    if not validate_target(address) or not 1 <= int(port) <= 65535:
        raise ValueError("Нет корректного адреса или порта модема.")
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        ip = None
    address = str(ip) if ip else address
    claim_reset(address, port)

    transport = Udp6TransportTarget if ip and ip.version == 6 else UdpTransportTarget
    target = await transport.create((address, int(port)), timeout=3.0, retries=0)
    with SnmpEngine() as engine:
        try:
            indication, status, _, _ = await asyncio.wait_for(set_cmd(
                engine, CommunityData(community, mpModel=1), target, ContextData(),
                ObjectType(ObjectIdentity(RESET_OID), Integer(1)), lookupMib=False,
            ), 4)
        except asyncio.TimeoutError:
            indication, status = True, None
    if indication:
        return {"outcome": "unknown", "message": f"Модем {address} не подтвердил команду. "
                "Перезагрузка могла начаться; проверьте пинг и статус.",
                "cooldown_seconds": COOLDOWN_SECONDS}
    if status:
        raise RuntimeError(f"Модем {address} отклонил команду: " + status.prettyPrint())
    return {"outcome": "accepted", "message": f"Модем {address} подтвердил команду перезагрузки.",
            "cooldown_seconds": COOLDOWN_SECONDS}
