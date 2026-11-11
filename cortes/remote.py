"""Acesso pelo celular: rede privada (Tailscale) + PIN. Desligado por padrão.

Regras:
- "Local" = conexão do próprio PC (loopback), com Host local e sem cabeçalhos de proxy. Não pede PIN.
- Todo o resto é "remoto": só é aceito se o acesso remoto estiver ligado, a origem estiver numa rede
  permitida (Tailscale 100.64.0.0/10; rede local só se permitida) e houver sessão aberta com o PIN.
- O PIN é guardado só como hash PBKDF2-SHA256 com sal; sessões vivem só na memória (fechar o .bat encerra).
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets
import socket
import threading
import time

from . import config

TAILSCALE = ipaddress.ip_network("100.64.0.0/10")
PRIVATE_LAN = [ipaddress.ip_network(n) for n in ("192.168.0.0/16", "10.0.0.0/8", "172.16.0.0/12")]
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
PROXY_HEADERS = ("X-Forwarded-For", "Forwarded", "Tailscale-User-Login", "X-Real-IP")
SESSION_TTL = 12 * 3600

_sessions: dict[str, float] = {}
_fails: dict[str, list[float]] = {}
_lock = threading.Lock()


def settings() -> dict:
    return config.load()["acesso_remoto"]


def hash_pin(pin: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, 200_000)
    return f"pbkdf2${salt.hex()}${dk.hex()}"


def check_pin(pin: str) -> bool:
    stored = settings().get("pin_hash") or ""
    try:
        _, salt, dk = stored.split("$")
    except ValueError:
        return False
    return hmac.compare_digest(hash_pin(pin, bytes.fromhex(salt)), stored)


def set_pin(pin: str) -> None:
    if not (pin.isdigit() and 6 <= len(pin) <= 12):
        raise ValueError("o PIN deve ter de 6 a 12 dígitos")
    cfg = config.load()
    cfg["acesso_remoto"]["pin_hash"] = hash_pin(pin)
    config.save(cfg)
    with _lock:
        _sessions.clear()  # trocar o PIN derruba as sessões abertas


def is_local(client_ip: str, host: str, headers) -> bool:
    try:
        loop = ipaddress.ip_address(client_ip).is_loopback
    except ValueError:
        return False
    return loop and host in LOCAL_HOSTS and not any(headers.get(h) for h in PROXY_HEADERS)


def network_allowed(client_ip: str) -> bool:
    s = settings()
    try:
        ip = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    if ip.is_loopback:  # proxy local (ex.: tailscale serve) — exige PIN como qualquer remoto
        return True
    if ip in TAILSCALE:
        return True
    return bool(s.get("permitir_rede_local")) and any(ip in n for n in PRIVATE_LAN)


def host_allowed(host: str) -> bool:
    s = settings()
    if host in LOCAL_HOSTS or host in s.get("hosts_permitidos", []) or host.endswith(".ts.net"):
        return True
    try:
        return network_allowed(host)  # acesso direto pelo IP do PC na rede permitida
    except ValueError:
        return False


def blocked(client_ip: str) -> bool:
    now = time.time()
    with _lock:
        recent = [t for t in _fails.get(client_ip, []) if now - t < 600]
        _fails[client_ip] = recent
        return len(recent) >= 5


def login(client_ip: str, pin: str) -> str | None:
    if blocked(client_ip):
        raise PermissionError("muitas tentativas; aguarde 10 minutos")
    if not check_pin(pin):
        with _lock:
            _fails.setdefault(client_ip, []).append(time.time())
        return None
    token = secrets.token_urlsafe(32)
    with _lock:
        _sessions[token] = time.time() + SESSION_TTL
    return token


def valid_session(cookie_header: str | None) -> bool:
    token = None
    for part in (cookie_header or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == "cortes_sessao":
            token = v
    if not token:
        return False
    with _lock:
        exp = _sessions.get(token)
        if exp and exp > time.time():
            return True
        _sessions.pop(token, None)
    return False


def addresses(port: int) -> dict:
    """Endereços pelos quais o celular pode acessar (mostrados no painel do PC)."""
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    ts = sorted(ip for ip in ips if ipaddress.ip_address(ip) in TAILSCALE)
    lan = sorted(ip for ip in ips if any(ipaddress.ip_address(ip) in n for n in PRIVATE_LAN))
    return {"tailscale": [f"http://{ip}:{port}" for ip in ts],
            "rede_local": [f"http://{ip}:{port}" for ip in lan] if settings().get("permitir_rede_local") else [],
            "tailscale_detectado": bool(ts)}
