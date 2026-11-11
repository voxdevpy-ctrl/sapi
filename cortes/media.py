"""Wrappers de FFmpeg/FFprobe, hash e recursos do sistema (prioridade baixa no Windows)."""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

BELOW_NORMAL = 0x00004000 if os.name == "nt" else 0


class MediaError(RuntimeError):
    pass


def run(cmd: list[str], timeout: int | None = None, cwd=None) -> subprocess.CompletedProcess:
    """Executa com prioridade abaixo do normal para não travar o uso do computador."""
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, creationflags=BELOW_NORMAL, cwd=cwd)


def probe(path: str | Path) -> dict:
    r = run(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)], 120)
    if r.returncode != 0:
        raise MediaError(f"ffprobe falhou ({r.returncode}): {r.stderr[-400:]}")
    d = json.loads(r.stdout)
    v = next((s for s in d.get("streams", []) if s.get("codec_type") == "video"), None)
    a = next((s for s in d.get("streams", []) if s.get("codec_type") == "audio"), None)
    fmt = d.get("format", {})
    return {
        "duration": float(fmt.get("duration") or 0),
        "size": int(fmt.get("size") or 0),
        "bit_rate": int(fmt.get("bit_rate") or 0),
        "video": None if not v else {"codec": v.get("codec_name"), "width": v.get("width"),
                                     "height": v.get("height"), "fps": v.get("avg_frame_rate")},
        "audio": None if not a else {"codec": a.get("codec_name"), "channels": a.get("channels"),
                                     "sample_rate": a.get("sample_rate")},
    }


def decode_check(path: str | Path, timeout: int = 1800) -> tuple[bool, str]:
    """Decodifica o arquivo inteiro (sem gravar) para provar que é reproduzível."""
    r = run(["ffmpeg", "-v", "error", "-xerror", "-i", str(path), "-f", "null", "-"], timeout)
    return r.returncode == 0 and not r.stderr.strip(), r.stderr[-400:]


def sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def volume_stats(path: str | Path) -> dict:
    r = run(["ffmpeg", "-hide_banner", "-i", str(path), "-af", "volumedetect", "-vn", "-f", "null", "-"], 900)
    mx = re.search(r"max_volume:\s*(-?[\d.]+)", r.stderr)
    mean = re.search(r"mean_volume:\s*(-?[\d.]+)", r.stderr)
    return {"max_db": float(mx.group(1)) if mx else None, "mean_db": float(mean.group(1)) if mean else None}


def detect(path: str | Path, kind: str) -> list[tuple[float, float]]:
    """Intervalos de tela preta ('black') ou silêncio ('silence')."""
    if kind == "black":
        args, pat = ["-vf", "blackdetect=d=0.5:pix_th=0.10", "-an"], r"black_start:([\d.]+)\s+black_end:([\d.]+)"
    else:
        args, pat = ["-af", "silencedetect=n=-45dB:d=2", "-vn"], None
    r = run(["ffmpeg", "-hide_banner", "-i", str(path), *args, "-f", "null", "-"], 900)
    if pat:
        return [(float(a), float(b)) for a, b in re.findall(pat, r.stderr)]
    starts = [float(x) for x in re.findall(r"silence_start:\s*([\d.]+)", r.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end:\s*([\d.]+)", r.stderr)]
    return list(zip(starts, ends + [float("inf")] * (len(starts) - len(ends))))


def free_disk_mb(path: str | Path) -> int:
    p = Path(path)
    while not p.exists():
        p = p.parent
    return shutil.disk_usage(p).free // (1 << 20)


def free_ram_mb() -> int:
    if os.name != "nt":
        return 4096

    class MS(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
    m = MS()
    m.dwLength = ctypes.sizeof(MS)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
    return m.ullAvailPhys // (1 << 20)


_JOB = None


def kill_children_on_exit() -> bool:
    """Coloca este processo num Job Object com KILL_ON_JOB_CLOSE: ao fechar a janela do .bat
    (ou o processo morrer), o Windows encerra junto todos os filhos — FFmpeg, Claude, Whisper."""
    global _JOB
    if os.name != "nt" or _JOB:
        return bool(_JOB)
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]

    class BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class IO(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in ("r", "w", "o", "rb", "wb", "ob")]

    class EXT(ctypes.Structure):
        _fields_ = [("Basic", BASIC), ("Io", IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    job = k32.CreateJobObjectW(None, None)
    info = EXT()
    info.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = (job and k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))  # 9 = ExtendedLimitInformation
          and k32.AssignProcessToJobObject(job, k32.GetCurrentProcess()))
    if ok:
        _JOB = job  # o handle precisa ficar aberto enquanto o processo vive
    return bool(ok)


def keep_awake(on: bool) -> None:
    """Pede ao Windows para não suspender enquanto este processo trabalha (não altera configurações;
    o pedido termina sozinho quando o processo fecha). Não impede a tela de apagar."""
    if os.name == "nt":
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))


def lower_own_priority() -> None:
    if os.name == "nt":
        h = ctypes.windll.kernel32.GetCurrentProcess()
        ctypes.windll.kernel32.SetPriorityClass(h, BELOW_NORMAL)


def ffmpeg(args: list[str], timeout: int | None = None) -> subprocess.CompletedProcess:
    return run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], timeout)
