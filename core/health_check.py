import socket
import ssl
import struct
import time
from dataclasses import dataclass


@dataclass
class CheckResult:
    name: str
    ok: bool | None
    latency_ms: float
    weight: float
    error: str = ""


WEIGHTS = {
    "https_discord.com":    0.35,
    "https_youtube.com":    0.30,
    "https_www.google.com": 0.20,
    "stun_google":          0.15,
}


def check_https(host: str, timeout: float = 8.0) -> CheckResult:
    """
    Открывает TCP, делает TLS-хендшейк через системный SSL (как curl),
    отправляет HEAD / и читает хотя бы один байт ответа.
    """
    name = f"https_{host}"
    start = time.perf_counter()
    sock = None
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        sock = socket.create_connection((host, 443), timeout=timeout)
        sock.settimeout(timeout)

        ssock = ctx.wrap_socket(sock, server_hostname=host)
        sock = ssock

        req = (
            f"HEAD / HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            f"User-Agent: ZapretManager/0.1\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        ).encode("ascii")
        ssock.sendall(req)

        data = ssock.recv(1)
        dt = (time.perf_counter() - start) * 1000

        if data:
            return CheckResult(name, True, dt, 1.0)
        else:
            return CheckResult(name, False, dt, 1.0, "empty response")
    except socket.timeout:
        dt = (time.perf_counter() - start) * 1000
        return CheckResult(name, False, dt, 1.0, "timeout")
    except ssl.SSLError as e:
        dt = (time.perf_counter() - start) * 1000
        return CheckResult(name, False, dt, 1.0, f"ssl: {e}")
    except Exception as e:
        dt = (time.perf_counter() - start) * 1000
        return CheckResult(name, False, dt, 1.0, str(e))
    finally:
        if sock:
            try:
                sock.close()
            except Exception:
                pass


def check_stun(host: str = "stun.l.google.com", port: int = 19302,
               timeout: float = 4.0) -> CheckResult:
    transaction_id = b"\x00" * 12
    packet = struct.pack(">HHI", 0x0001, 0x0000, 0x2112A442) + transaction_id
    start = time.perf_counter()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(timeout)
            s.sendto(packet, (host, port))
            data, _ = s.recvfrom(1024)
        dt = (time.perf_counter() - start) * 1000
        ok = len(data) >= 20 and data[0] == 0x01
        return CheckResult("stun_google", ok, dt, 1.0,
                           "" if ok else f"bad resp ({len(data)} b)")
    except Exception as e:
        dt = (time.perf_counter() - start) * 1000
        return CheckResult("stun_google", False, dt, 1.0, str(e))


def run_all_checks() -> list[CheckResult]:
    return [
        check_https("discord.com"),
        check_https("youtube.com"),
        check_https("www.google.com"),
        check_stun("stun.l.google.com"),
    ]


def score(results: list[CheckResult]) -> float:
    total_weight = 0.0
    earned = 0.0
    for r in results:
        w = WEIGHTS.get(r.name, 0.1)
        if r.ok is None:
            continue
        total_weight += w
        if r.ok:
            latency_penalty = min(r.latency_ms / 1000.0 * 20.0, 60.0)
            earned += w * (100.0 - latency_penalty)
    if total_weight == 0:
        return 0.0
    return round(earned / total_weight, 2)