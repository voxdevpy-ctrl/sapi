"""Cota de armazenamento (10/11/2026): tudo dentro de PRODUCAO soma no limite.

- Ao chegar no limite (padrão 10 GB), a produção pesada PAUSA: nada novo é baixado, renderizado etc.
  Ela volta sozinha assim que o uso cair abaixo do limite (retenção de 14 dias, exclusão pós-postagem
  ou você apagando arquivos).
- A tolerância (padrão 1 GB) é só para terminar o que já estava em andamento: nenhuma etapa pode levar
  o uso acima de limite + tolerância (11 GB). Cada etapa que grava arquivo estima quanto vai gravar antes
  de começar, e download e compressão também são vigiados durante a gravação.
"""
from __future__ import annotations

import os
import time

from . import config

GB = 1 << 30
MB = 1 << 20

# etapas que só leem/escrevem texto ou banco: continuam mesmo com o limite atingido
LEVES = {"PESQUISA", "VERIFICACAO", "ANALISE", "SELECAO", "TEXTOS", "PUBLICACAO", "PG_BUSCA"}
# a compressão diminui o uso no fim (o original sai depois da validação): pode rodar pausada, dentro do teto
REDUZ_USO = {"COMPRESSAO"}
# quanto cada etapa pode gravar no máximo, quando não há estimativa melhor
RESERVA = {"PREVIA": 200 * MB, "LEGENDAS": 20 * MB, "FINALIZACAO": 400 * MB, "QC": 50 * MB,
           "ORGANIZACAO": 400 * MB}


class LimitReached(Exception):
    """A etapa estouraria o teto de armazenamento: fica em espera, sem gastar tentativa."""


_cache = (0.0, 0)


def usage_bytes(fresh: bool = False) -> int:
    global _cache
    if not fresh and time.time() - _cache[0] < 5:
        return _cache[1]
    total = 0
    for root, _dirs, files in os.walk(config.PROD):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    _cache = (time.time(), total)
    return total


def limits() -> tuple[int, int]:
    a = config.load()["armazenamento"]
    lim = int(float(a["limite_gb"]) * GB)
    return lim, lim + int(float(a["tolerancia_gb"]) * GB)


def paused() -> bool:
    return usage_bytes() >= limits()[0]


def room() -> int:
    """Bytes que ainda podem ser gravados até o teto (limite + tolerância)."""
    return limits()[1] - usage_bytes(fresh=True)


def ensure(extra: int, what: str) -> None:
    lim, cap = limits()
    used = usage_bytes(fresh=True)
    if used + extra > cap:
        raise LimitReached(f"armazenamento: {what} precisaria de ~{extra / GB:.2f} GB e o uso está em "
                           f"{used / GB:.2f} GB (teto {cap / GB:.0f} GB); aguardando espaço ser liberado")


def stage_allowed(stage: str) -> bool:
    return stage in LEVES or stage in REDUZ_USO or not paused()


def status() -> dict:
    lim, cap = limits()
    used = usage_bytes()
    return {"usado": used, "limite": lim, "teto": cap, "pausado": used >= lim,
            "pct": round(100 * used / lim, 1) if lim else 0}
