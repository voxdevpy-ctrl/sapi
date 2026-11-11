"""Cópia opcional na nuvem via rclone (gratuito, MIT), antes de apagar um vídeo já postado.

O provedor (ex.: MEGA 20 GB grátis, Koofr 10 GB grátis) é configurado pelo próprio usuário com
`rclone config`, que pede login e senha no terminal — a Central nunca vê essas credenciais.
Se o arquivo de configuração do rclone for criptografado, a senha dele fica guardada com a
proteção de dados do Windows (DPAPI, só sua conta neste PC consegue abrir), nunca em texto puro.
"""
from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
from ctypes import wintypes
from pathlib import Path

from . import config

PASS_FILE = "rclone_senha.dpapi"


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, protect: bool) -> bytes:
    buf = ctypes.create_string_buffer(data, len(data))
    src, dst = _Blob(len(data), buf), _Blob()
    fn = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    if not fn(ctypes.byref(src), None, None, None, None, 0, ctypes.byref(dst)):
        raise OSError("falha na proteção de dados do Windows")
    try:
        return ctypes.string_at(dst.pbData, dst.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(dst.pbData)


def save_config_password(pw: str) -> None:
    (config.DIRS["config"] / PASS_FILE).write_bytes(_dpapi(pw.encode("utf-8"), True))


def _env() -> dict:
    env = dict(os.environ)
    f = config.DIRS["config"] / PASS_FILE
    if f.exists():
        env["RCLONE_CONFIG_PASS"] = _dpapi(f.read_bytes(), False).decode("utf-8")
    env.pop("RCLONE_PASSWORD_COMMAND", None)
    return env


def rclone_exe() -> str:
    exe = shutil.which("rclone")
    if not exe:
        links = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links" / "rclone.exe"
        exe = str(links) if links.exists() else None
    if not exe:  # instalado pelo winget sem atalho no PATH
        pk = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
        exe = next((str(p) for p in sorted(pk.glob("Rclone.Rclone_*/rclone-*/rclone.exe"), reverse=True)), None)
    if not exe:
        raise FileNotFoundError("rclone não instalado (winget install Rclone.Rclone)")
    return exe


def _run(args: list[str], timeout: int = 1800) -> str:
    r = subprocess.run([rclone_exe(), *args], capture_output=True, text=True, timeout=timeout, env=_env(),
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode != 0:
        # a saída do rclone não contém a senha; ainda assim só os últimos 300 caracteres vão para o log
        raise RuntimeError(f"rclone {args[0]} falhou: {(r.stderr or r.stdout)[-300:]}")
    return r.stdout


def upload(path: Path, rel: str) -> str:
    """Envia e confere o tamanho do arquivo remoto. Devolve o caminho remoto."""
    n = config.load()["nuvem"]
    if not n["rclone_remote"]:
        raise ValueError("nuvem ligada, mas 'rclone_remote' está vazio")
    dest = f"{n['rclone_remote'].rstrip(':')}:{n['pasta'].strip('/')}/{rel}"
    _run(["copyto", str(path), dest, "--retries", "3"])
    info = json.loads(_run(["lsjson", dest], timeout=300) or "[]")
    if not info or int(info[0].get("Size", -1)) != path.stat().st_size:
        raise RuntimeError("tamanho na nuvem não confere com o arquivo local")
    return dest


def test() -> str:
    n = config.load()["nuvem"]
    _run(["lsd", f"{n['rclone_remote'].rstrip(':')}:"], timeout=120)
    return "ok"
