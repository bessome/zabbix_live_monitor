"""Shared one-second ICMP probes for open device pages."""

from collections import deque
import ipaddress
import math
import platform
import re
import subprocess
import threading
import time

PAYLOAD_BYTES = 128
INTERVAL_SECONDS = 1.0
WINDOW_SECONDS = 60
IDLE_SECONDS = 10
MAX_MONITORS = 80

_monitors = {}
_registry_lock = threading.Lock()
_RTT = re.compile(r"([=<])\s*(\d+(?:[.,]\d+)?)\s*(?:ms|мс)\b", re.IGNORECASE)


def validate_target(address):
    """Accept only a Zabbix interface IP or a simple DNS hostname."""
    if not address or len(address) > 253:
        return False
    try:
        ipaddress.ip_address(address)
        return True
    except ValueError:
        return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", address))


def probe(address):
    """Send one ICMP echo with 128 data bytes; return RTT or None on loss."""
    if platform.system() == "Windows":
        command = ["ping", "-n", "1", "-l", str(PAYLOAD_BYTES),
                   "-w", "900", address]
    else:
        command = ["ping", "-c", "1", "-s", str(PAYLOAD_BYTES),
                   "-W", "1", address]
    try:
        result = subprocess.run(command, capture_output=True, text=True,
                                errors="replace", timeout=1.5, check=False)
    except subprocess.TimeoutExpired:
        return None
    except FileNotFoundError as exc:
        raise RuntimeError("Системная утилита ping не найдена.") from exc
    if result.returncode != 0:
        if any(message in result.stderr.casefold()
               for message in ("operation not permitted", "permission denied")):
            raise RuntimeError("Контейнеру не разрешена отправка ICMP-пакетов.")
        return None
    match = _RTT.search(result.stdout)
    if match is None:
        raise RuntimeError("Ответ ICMP получен, но время отклика не распознано.")
    value = float(match.group(2).replace(",", "."))
    return 0.5 if match.group(1) == "<" else value


def summarize(samples, now=None):
    """Summarize the trailing minute; failed echoes are marked as losses."""
    now = time.time() if now is None else now
    window = [sample for sample in samples if sample["at"] >= now - WINDOW_SECONDS]
    values = [sample["ms"] for sample in window if sample["ms"] is not None]
    sent = len(window)
    lost = sent - len(values)
    last = window[-1]["ms"] if window else None
    return {
        "samples": window,
        "server_time": now,
        "sent": sent,
        "received": len(values),
        "lost": lost,
        "loss_pct": round(lost * 100 / sent, 1) if sent else 0.0,
        "current_ms": last,
        "min_ms": round(min(values), 2) if values else None,
        "avg_ms": round(math.fsum(values) / len(values), 2) if values else None,
        "max_ms": round(max(values), 2) if values else None,
        "payload_bytes": PAYLOAD_BYTES,
        "window_seconds": WINDOW_SECONDS,
    }


class PingMonitor:
    def __init__(self, key, address):
        self.key = key
        self.address = address
        self.samples = deque(maxlen=WINDOW_SECONDS + 2)
        self.lock = threading.Lock()
        self.last_seen = time.monotonic()
        self.error = None
        self.thread = threading.Thread(target=self._run, daemon=True,
                                       name="ping-" + key[0])

    def start(self):
        self.thread.start()

    def snapshot(self):
        with self.lock:
            self.last_seen = time.monotonic()
            samples = list(self.samples)
            error = self.error
        result = summarize(samples)
        result["error"] = error
        result["address"] = self.address
        return result

    def _run(self):
        next_probe = time.monotonic()
        try:
            while True:
                with self.lock:
                    if time.monotonic() - self.last_seen > IDLE_SECONDS:
                        break
                try:
                    latency = probe(self.address)
                except RuntimeError as exc:
                    with self.lock:
                        self.error = str(exc)
                    while True:
                        with self.lock:
                            if time.monotonic() - self.last_seen > IDLE_SECONDS:
                                break
                        time.sleep(INTERVAL_SECONDS)
                    break
                with self.lock:
                    self.samples.append({"at": time.time(), "ms": latency})
                next_probe += INTERVAL_SECONDS
                delay = next_probe - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                else:
                    next_probe = time.monotonic()
        finally:
            with _registry_lock:
                if _monitors.get(self.key) is self:
                    del _monitors[self.key]


def snapshot_for(host_id, address):
    if not validate_target(address):
        raise ValueError("У устройства нет подходящего IP-адреса или DNS-имени для пинга.")
    key = (str(host_id), address)
    with _registry_lock:
        monitor = _monitors.get(key)
        if monitor is None:
            if len(_monitors) >= MAX_MONITORS:
                raise RuntimeError("Слишком много активных проверок пинга.")
            monitor = PingMonitor(key, address)
            _monitors[key] = monitor
            monitor.start()
    return monitor.snapshot()
